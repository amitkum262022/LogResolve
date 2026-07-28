"""
Local persistence for LLM sidebar settings.

Settings are stored under ``.local/llm_settings.json`` (gitignored). This file
may contain API keys — keep it on the local machine only; never commit or share it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SETTINGS_DIR = Path(__file__).resolve().parent / ".local"
SETTINGS_PATH = SETTINGS_DIR / "llm_settings.json"


def load_llm_settings() -> dict[str, Any]:
    """Load saved LLM settings, or ``{}`` if missing / unreadable."""
    try:
        if not SETTINGS_PATH.is_file():
            return {}
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save_llm_settings(settings: dict[str, Any]) -> Path:
    """
    Persist LLM settings to the local JSON file.

    Returns:
        Path written.
    """
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    # Restrictive permissions on POSIX; ignored on Windows.
    payload = {k: v for k, v in settings.items() if v is not None}
    SETTINGS_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        SETTINGS_PATH.chmod(0o600)
    except OSError:
        pass
    return SETTINGS_PATH


def clear_llm_settings() -> None:
    """Delete the local settings file if it exists."""
    try:
        if SETTINGS_PATH.is_file():
            SETTINGS_PATH.unlink()
    except OSError:
        pass


def settings_file_exists() -> bool:
    return SETTINGS_PATH.is_file()
