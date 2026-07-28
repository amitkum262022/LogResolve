"""
Shared service-layer helpers for the FastAPI LogResolve UI.

Keeps load / index / explore / analyze logic separate from HTTP routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ingestion import LooseFilesUploadSource
from llm_factory import LLMConfig, validate_llm_config
from masking import SensitiveDataMasker
from parser import OpenPagesZipParser
from timestamp_index import TimestampIndexer
from graph import get_workflow


@dataclass
class BundleState:
    """In-memory state for one browser session after a bundle is loaded."""

    categories: dict[str, str] = field(default_factory=dict)
    indexes: dict[str, list[dict]] = field(default_factory=dict)
    token_maps: dict[str, dict[str, str]] = field(default_factory=dict)
    masked_categories: dict[str, str] = field(default_factory=dict)
    selected_categories: list[str] = field(default_factory=list)
    do_mask: bool = True
    analysis_results: list[dict] = field(default_factory=list)
    last_llm_label: str = ""
    analysis_partial: bool = False
    analysis_total_found: int = 0
    max_chunks: int = 10


def load_bundle_bytes(file_payloads: list[tuple[str, bytes]], do_mask: bool = True) -> BundleState:
    """
    Pack uploaded files into a zip, extract, mask, and index.

    Args:
        file_payloads: list of (filename, raw_bytes)
        do_mask: whether to mask before indexing
    """

    class _MemFile:
        def __init__(self, name: str, data: bytes) -> None:
            self.name = name
            self._data = data

        def getvalue(self) -> bytes:
            return self._data

    uploads = [_MemFile(n, d) for n, d in file_payloads]
    zip_bytes = LooseFilesUploadSource(uploads).fetch_bundle()
    parser = OpenPagesZipParser(zip_bytes)
    categories = parser.extract_bundle()

    indexer = TimestampIndexer()
    masker = SensitiveDataMasker()
    indexes: dict[str, list[dict]] = {}
    token_maps: dict[str, dict[str, str]] = {}
    masked_categories: dict[str, str] = {}

    for cat, raw in categories.items():
        if do_mask:
            masked, token_map = masker.mask(raw)
            masked_categories[cat] = masked
            token_maps[cat] = token_map
            indexes[cat] = indexer.build_index(cat, masked)
        else:
            masked_categories[cat] = raw
            token_maps[cat] = {}
            indexes[cat] = indexer.build_index(cat, raw)

    return BundleState(
        categories=categories,
        indexes=indexes,
        token_maps=token_maps,
        masked_categories=masked_categories,
        selected_categories=sorted(categories.keys()),
        do_mask=do_mask,
    )


def round_robin_chunks(
    by_category: dict[str, list[tuple[str, dict, dict]]],
    limit: int,
) -> list[tuple[str, dict, dict]]:
    if limit <= 0:
        return []
    queues = {cat: list(items) for cat, items in by_category.items() if items}
    if not queues:
        return []
    planned: list[tuple[str, dict, dict]] = []
    order = sorted(queues.keys())
    while len(planned) < limit and queues:
        progressed = False
        for cat in list(order):
            if cat not in queues:
                continue
            planned.append(queues[cat].pop(0))
            progressed = True
            if not queues[cat]:
                del queues[cat]
                order = [c for c in order if c in queues]
            if len(planned) >= limit:
                break
        if not progressed:
            break
    return planned


def correlate_chunk_timestamp(
    state: BundleState, category: str, start_line: int
) -> Optional[datetime]:
    index = state.indexes.get(category) or []
    best: Optional[datetime] = None
    for entry in index:
        if entry.get("line_number", 0) > start_line:
            break
        ts = entry.get("timestamp")
        if isinstance(ts, datetime):
            best = ts
    return best


def explore_lines(
    state: BundleState,
    *,
    selected_categories: list[str],
    category_filter: str = "",
    keyword: str = "",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    reveal: bool = False,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    indexer = TimestampIndexer()
    masker = SensitiveDataMasker()
    selected = set(selected_categories)

    if category_filter and category_filter not in ("", "(all selected)"):
        cats = [category_filter] if category_filter in selected else []
    else:
        cats = [c for c in sorted(state.indexes.keys()) if c in selected]

    working: list[dict] = []
    for cat in cats:
        working.extend(state.indexes.get(cat) or [])
    working.sort(key=lambda e: (e.get("category", ""), e.get("line_number", 0)))

    if start is not None or end is not None:
        working = indexer.filter_by_range(working, start, end)
    if keyword:
        working = indexer.filter_by_keyword(working, keyword)

    rows: list[dict[str, Any]] = []
    for entry in working[:limit]:
        line = entry.get("line", "")
        cat = entry.get("category", "")
        if reveal:
            token_map = state.token_maps.get(cat, {})
            if token_map:
                line = masker.unmask(line, token_map)
        ts = entry.get("timestamp")
        rows.append(
            {
                "category": cat,
                "line_number": entry.get("line_number"),
                "timestamp": ts.isoformat() if isinstance(ts, datetime) else "",
                "line": line,
            }
        )
    return rows


def build_work_items(
    state: BundleState, selected_categories: list[str]
) -> dict[str, list[tuple[str, dict, dict]]]:
    masker = SensitiveDataMasker()
    selected = set(selected_categories)
    work_by_cat: dict[str, list[tuple[str, dict, dict]]] = {}
    for cat, raw in state.categories.items():
        if cat not in selected:
            continue
        if state.do_mask:
            masked, token_map = masker.mask(raw)
        else:
            masked, token_map = raw, {}
        chunks = OpenPagesZipParser.chunk_errors(masked)
        work_by_cat[cat] = [(cat, ch, token_map) for ch in chunks]
    return work_by_cat


def analyze_one_chunk(
    llm_config: LLMConfig,
    cat: str,
    chunk: dict,
    token_map: dict,
    state: BundleState,
    llm_run_label: str,
) -> dict:
    missing = validate_llm_config(llm_config)
    if missing:
        raise ValueError(
            f"Incomplete {llm_config.display_name()} configuration: "
            + ", ".join(missing)
        )

    workflow = get_workflow(llm_config)
    start_line = int(chunk.get("start_line") or 1)
    ts = correlate_chunk_timestamp(state, cat, start_line)
    label = f"{cat} @ line {start_line}"
    if ts is not None:
        label = f"{cat} @ {ts.isoformat()}"

    try:
        final_state = workflow.invoke(
            {
                "raw_log_chunk": chunk["text"],
                "log_type": cat,
                "extracted_exceptions": "",
                "root_cause_diagnosis": "",
                "resolution_steps": "",
                "validation_passed": False,
                "retry_count": 0,
            }
        )
        return {
            "label": label,
            "category": cat,
            "timestamp": ts.isoformat() if isinstance(ts, datetime) else None,
            "start_line": start_line,
            "chunk_text": chunk.get("text", ""),
            "extracted_exceptions": final_state.get("extracted_exceptions", ""),
            "root_cause_diagnosis": final_state.get("root_cause_diagnosis", ""),
            "resolution_steps": final_state.get("resolution_steps", ""),
            "validation_passed": bool(final_state.get("validation_passed")),
            "retry_count": int(final_state.get("retry_count") or 0),
            "error": None,
            "llm_label": llm_run_label,
        }
    except Exception as exc:
        return {
            "label": label,
            "category": cat,
            "timestamp": ts.isoformat() if isinstance(ts, datetime) else None,
            "start_line": start_line,
            "chunk_text": chunk.get("text", ""),
            "extracted_exceptions": "",
            "root_cause_diagnosis": "",
            "resolution_steps": "",
            "validation_passed": False,
            "retry_count": 0,
            "error": str(exc),
            "llm_label": llm_run_label,
        }
