"""
Stateful cyclical LangGraph workflow for OpenPages log root-cause analysis.

Operates exclusively on *masked* log text. The graph always terminates: failed
validation loops back to diagnosis up to ``MAX_VALIDATION_RETRIES``, then
force-passes so the UI never hangs on a non-terminating cycle.

LLM backend is pluggable via ``llm_factory.LLMConfig`` (OpenAI, Anthropic,
Gemini, watsonx, or local Ollama).
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from llm_factory import LLMConfig, build_chat_model


MAX_VALIDATION_RETRIES = 3

# Re-export defaults for callers that still import them from graph.
from llm_factory import (  # noqa: E402
    DEFAULT_MODELS,
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_WATSONX_URL,
)

DEFAULT_MODEL_ID = DEFAULT_MODELS["watsonx"]


class OpenPagesLogState(TypedDict):
    """Shared state passed between LangGraph nodes."""

    raw_log_chunk: str
    log_type: str
    extracted_exceptions: str
    root_cause_diagnosis: str
    resolution_steps: str
    validation_passed: bool
    retry_count: int


def _safe_content(response) -> str:
    """Extract string content from an LLM response, tolerating edge shapes."""
    content = getattr(response, "content", response)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
        return "\n".join(parts).strip()
    return str(content).strip()


def get_workflow(llm_config: LLMConfig):
    """
    Build, compile, and return the OpenPages log-analysis LangGraph.

    Args:
        llm_config: Provider credentials and model settings. Validated and
            constructed via ``llm_factory.build_chat_model``.

    Returns:
        A compiled LangGraph runnable ready for ``.invoke(state)``.
    """
    if llm_config is None:
        raise ValueError("llm_config is required to build the workflow")

    llm = build_chat_model(llm_config)

    # ------------------------------------------------------------------ nodes
    def parse_node(state: OpenPagesLogState) -> dict:
        """Strip routine INFO noise; keep exception / stack-trace lines."""
        system = SystemMessage(
            content=(
                "You are an expert OpenPages / Java log parser. "
                "Given a masked OpenPages log chunk, strip routine INFO and DEBUG "
                "lines. Keep ONLY lines related to exceptions, stack traces, "
                "FATAL/ERROR/CRITICAL events, 'Caused by:' chains, and directly "
                "relevant WARN context. Return the cleaned excerpt only — no "
                "commentary, no markdown fences."
            )
        )
        human = HumanMessage(
            content=(
                f"Log type / category: {state.get('log_type', 'unknown')}\n\n"
                f"Log chunk:\n{state.get('raw_log_chunk', '')}"
            )
        )
        try:
            response = llm.invoke([system, human])
            extracted = _safe_content(response)
        except Exception as exc:  # surface API failures into state, don't crash
            extracted = (
                f"[parse_node error] Failed to call LLM: {exc}\n\n"
                f"Falling back to raw chunk:\n{state.get('raw_log_chunk', '')}"
            )
        if not extracted:
            extracted = state.get("raw_log_chunk", "") or "(no exception content)"
        return {"extracted_exceptions": extracted}

    def diagnose_node(state: OpenPagesLogState) -> dict:
        """Identify the likely OpenPages root-cause component."""
        prior = state.get("root_cause_diagnosis") or ""
        retry = int(state.get("retry_count") or 0)
        revision_hint = ""
        if retry > 0 and prior:
            revision_hint = (
                f"\n\nA previous diagnosis failed validation (attempt {retry}). "
                f"Revise and improve upon this prior diagnosis:\n{prior}\n"
                "Address the gaps that made it non-actionable or too generic."
            )

        system = SystemMessage(
            content=(
                "You are a senior IBM OpenPages reliability engineer. "
                "Diagnose the ROOT CAUSE of the provided (masked) exception "
                "evidence. Prefer one of these OpenPages-specific failure modes "
                "when they fit:\n"
                "  1. DB / JDBC connection pool exhaustion\n"
                "  2. Cognos metadata sync failure\n"
                "  3. Solr global search indexing drop / core corruption\n"
                "  4. WebSphere / Liberty keystore or SSL mismatch\n"
                "  5. ObjectManager load / schema / module deployment failure\n"
                "Name the component, explain the mechanism, and cite the masked "
                "evidence. Be specific to OpenPages — avoid generic Java advice."
            )
        )
        human = HumanMessage(
            content=(
                f"Log type / category: {state.get('log_type', 'unknown')}\n\n"
                f"Exception evidence:\n{state.get('extracted_exceptions', '')}"
                f"{revision_hint}"
            )
        )
        try:
            response = llm.invoke([system, human])
            diagnosis = _safe_content(response)
        except Exception as exc:
            diagnosis = f"[diagnose_node error] Failed to call LLM: {exc}"
        if not diagnosis:
            diagnosis = "Unable to determine root cause from the provided evidence."
        return {"root_cause_diagnosis": diagnosis}

    def fix_node(state: OpenPagesLogState) -> dict:
        """Produce a numbered OpenPages-specific resolution playbook."""
        system = SystemMessage(
            content=(
                "You are an IBM OpenPages support engineer writing a resolution "
                "playbook. Given a root-cause diagnosis and exception evidence, "
                "produce a NUMBERED list of concrete remediation steps. "
                "Where relevant, reference:\n"
                "  - <OP_HOME> paths and configuration\n"
                "  - ObjectManager CLI commands\n"
                "  - WebSphere Liberty server.xml (SSL, keystores, endpoints)\n"
                "  - JDBC datasource / connection pool configuration\n"
                "  - Cognos dispatcher restarts\n"
                "  - Solr re-indexing procedures\n"
                "  - Cluster restart sequencing (order matters)\n"
                "End with explicit verification steps. Do not invent secrets or "
                "credentials; use placeholders. Output the playbook only."
            )
        )
        human = HumanMessage(
            content=(
                f"Log type / category: {state.get('log_type', 'unknown')}\n\n"
                f"Root cause diagnosis:\n{state.get('root_cause_diagnosis', '')}\n\n"
                f"Exception evidence:\n{state.get('extracted_exceptions', '')}"
            )
        )
        try:
            response = llm.invoke([system, human])
            steps = _safe_content(response)
        except Exception as exc:
            steps = f"[fix_node error] Failed to call LLM: {exc}"
        if not steps:
            steps = "No resolution steps could be generated."
        return {"resolution_steps": steps}

    def validate_node(state: OpenPagesLogState) -> dict:
        """
        LLM judges whether the playbook is OpenPages-specific and actionable.

        Caps retries at ``MAX_VALIDATION_RETRIES`` and force-passes thereafter
        so the graph always reaches END.
        """
        retry = int(state.get("retry_count") or 0)

        # Hard cap: force-pass so the cyclical graph cannot loop forever.
        if retry >= MAX_VALIDATION_RETRIES:
            return {
                "validation_passed": True,
                "retry_count": retry,
            }

        system = SystemMessage(
            content=(
                "You are a strict reviewer of OpenPages remediation playbooks. "
                "Judge PASS or FAIL. PASS only if the playbook is:\n"
                "  (a) OpenPages-specific (not generic Java boilerplate),\n"
                "  (b) actionable (concrete commands/paths/config keys),\n"
                "  (c) aligned with the stated root cause.\n"
                "Reply with exactly one line starting with PASS or FAIL, "
                "optionally followed by a short reason."
            )
        )
        human = HumanMessage(
            content=(
                f"Diagnosis:\n{state.get('root_cause_diagnosis', '')}\n\n"
                f"Playbook:\n{state.get('resolution_steps', '')}"
            )
        )
        passed = False
        try:
            response = llm.invoke([system, human])
            verdict = _safe_content(response).upper()
            # Accept PASS at start of response; otherwise treat as FAIL.
            passed = bool(re.match(r"^\s*PASS\b", verdict))
        except Exception:
            # On validator API failure, force-pass to avoid infinite retries.
            passed = True

        new_retry = retry if passed else retry + 1
        # If this failure would exceed the cap on the next loop, force-pass now.
        if not passed and new_retry >= MAX_VALIDATION_RETRIES:
            passed = True

        return {
            "validation_passed": passed,
            "retry_count": new_retry,
        }

    def route_after_validate(
        state: OpenPagesLogState,
    ) -> Literal["diagnose_node", "__end__"]:
        """Conditional edge: END on pass, otherwise loop back to diagnose."""
        if state.get("validation_passed"):
            return "__end__"
        return "diagnose_node"

    # ----------------------------------------------------------------- graph
    workflow = StateGraph(OpenPagesLogState)
    workflow.add_node("parse_node", parse_node)
    workflow.add_node("diagnose_node", diagnose_node)
    workflow.add_node("fix_node", fix_node)
    workflow.add_node("validate_node", validate_node)

    workflow.set_entry_point("parse_node")
    workflow.add_edge("parse_node", "diagnose_node")
    workflow.add_edge("diagnose_node", "fix_node")
    workflow.add_edge("fix_node", "validate_node")
    workflow.add_conditional_edges(
        "validate_node",
        route_after_validate,
        {
            "diagnose_node": "diagnose_node",
            "__end__": END,
        },
    )

    return workflow.compile()


__all__ = [
    "MAX_VALIDATION_RETRIES",
    "DEFAULT_WATSONX_URL",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_MODEL_ID",
    "DEFAULT_MODELS",
    "OpenPagesLogState",
    "get_workflow",
]
