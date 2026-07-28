"""
Multi-provider chat-model factory for the OpenPages Log Analysis Assistant.

Supported providers: openai, anthropic, gemini, watsonx, ollama (local Llama).
Provider SDKs are imported lazily so missing packages only fail when selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel


LLMProvider = Literal["openai", "anthropic", "gemini", "watsonx", "ollama"]

PROVIDER_LABELS: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Gemini",
    "watsonx": "watsonx",
    "ollama": "Local (Ollama)",
}

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.0-flash",
    # Default must be a chat model commonly available on watsonx projects.
    "watsonx": "ibm/granite-4-h-small",
    "ollama": "llama3.1",
}

# Suggested chat / instruct models (not embeddings or TTM). Users can always
# type any model ID manually in the UI — availability depends on the project.
WATSONX_MODEL_SUGGESTIONS: list[str] = [
    "ibm/granite-4-h-small",
    "meta-llama/llama-3-3-70b-instruct",
    "meta-llama/llama-3-1-8b",
    "meta-llama/llama-3-1-70b-gptq",
    "meta-llama/llama-4-maverick-17b-128e-instruct-fp8",
    "mistralai/mistral-small-3-1-24b-instruct-2503",
    "mistralai/mistral-medium-2505",
]

DEFAULT_WATSONX_URL = "https://us-south.ml.cloud.ibm.com"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


@dataclass
class LLMConfig:
    """Credentials and model settings for one LLM provider."""

    provider: LLMProvider
    model: str = ""
    api_key: str = ""
    project_id: str = ""
    url: str = DEFAULT_WATSONX_URL
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    # Extra kwargs reserved for future provider-specific options.
    extras: dict[str, Any] = field(default_factory=dict)

    def resolved_model(self) -> str:
        return (self.model or DEFAULT_MODELS.get(self.provider, "")).strip()

    def display_name(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)


def validate_llm_config(config: LLMConfig) -> list[str]:
    """
    Return human-readable names of missing required fields.

    Empty list means the config is complete enough to attempt ``build_chat_model``.
    """
    missing: list[str] = []
    provider = (config.provider or "").strip().lower()

    if provider not in DEFAULT_MODELS:
        return [f"unsupported provider ({config.provider!r})"]

    if not config.resolved_model():
        missing.append("model")

    if provider in ("openai", "anthropic", "gemini"):
        if not (config.api_key or "").strip():
            missing.append("API key")
    elif provider == "watsonx":
        if not (config.api_key or "").strip():
            missing.append("API key")
        if not (config.project_id or "").strip():
            missing.append("project ID")
        if not (config.url or "").strip():
            missing.append("URL")
    elif provider == "ollama":
        if not (config.base_url or "").strip():
            missing.append("base URL")

    return missing


def build_chat_model(config: LLMConfig) -> BaseChatModel:
    """
    Construct a temperature-0 chat model for the selected provider.

    Raises:
        ValueError: If required config fields are missing.
        ImportError: If the provider's LangChain package is not installed.
    """
    missing = validate_llm_config(config)
    if missing:
        raise ValueError(
            f"Incomplete {config.display_name()} configuration: missing "
            + ", ".join(missing)
        )

    provider = config.provider.strip().lower()
    model = config.resolved_model()

    if provider == "openai":
        return _build_openai(model=model, api_key=config.api_key.strip())
    if provider == "anthropic":
        return _build_anthropic(model=model, api_key=config.api_key.strip())
    if provider == "gemini":
        return _build_gemini(model=model, api_key=config.api_key.strip())
    if provider == "watsonx":
        return _build_watsonx(
            model=model,
            api_key=config.api_key.strip(),
            project_id=config.project_id.strip(),
            url=config.url.strip(),
        )
    if provider == "ollama":
        return _build_ollama(model=model, base_url=config.base_url.strip())

    raise ValueError(f"Unsupported LLM provider: {config.provider!r}")


def _build_openai(model: str, api_key: str) -> BaseChatModel:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "langchain-openai is required for OpenAI. "
            "Install with: pip install langchain-openai"
        ) from exc
    return ChatOpenAI(model=model, api_key=api_key, temperature=0)


def _build_anthropic(model: str, api_key: str) -> BaseChatModel:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:
        raise ImportError(
            "langchain-anthropic is required for Anthropic. "
            "Install with: pip install langchain-anthropic"
        ) from exc
    return ChatAnthropic(model=model, api_key=api_key, temperature=0)


def _build_gemini(model: str, api_key: str) -> BaseChatModel:
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:
        raise ImportError(
            "langchain-google-genai is required for Gemini. "
            "Install with: pip install langchain-google-genai"
        ) from exc
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=0,
    )


def _build_watsonx(
    model: str, api_key: str, project_id: str, url: str
) -> BaseChatModel:
    try:
        from langchain_ibm import ChatWatsonx
    except ImportError as exc:
        raise ImportError(
            "langchain-ibm is required for watsonx. "
            "Install with: pip install langchain-ibm ibm-watsonx-ai"
        ) from exc
    params: dict[str, Any] = {
        "decoding_method": "greedy",
        "temperature": 0,
        "max_new_tokens": 2048,
        "repetition_penalty": 1.05,
    }
    return ChatWatsonx(
        model_id=model,
        url=url,
        project_id=project_id,
        apikey=api_key,
        params=params,
    )


def _build_ollama(model: str, base_url: str) -> BaseChatModel:
    """Local Llama (or any Ollama model) via the OpenAI-compatible API."""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "langchain-openai is required for Ollama (OpenAI-compatible client). "
            "Install with: pip install langchain-openai"
        ) from exc
    # Ollama ignores the key but the OpenAI client requires a non-empty value.
    return ChatOpenAI(
        model=model,
        api_key="ollama",
        base_url=base_url,
        temperature=0,
    )


__all__ = [
    "LLMProvider",
    "LLMConfig",
    "PROVIDER_LABELS",
    "DEFAULT_MODELS",
    "WATSONX_MODEL_SUGGESTIONS",
    "DEFAULT_WATSONX_URL",
    "DEFAULT_OLLAMA_BASE_URL",
    "validate_llm_config",
    "build_chat_model",
]
