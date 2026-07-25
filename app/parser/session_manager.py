from __future__ import annotations

from pathlib import Path

from app.config import get_settings


def get_storage_state_path() -> Path:
    settings = get_settings()
    settings.playwright_storage_state.parent.mkdir(parents=True, exist_ok=True)
    return settings.playwright_storage_state


def has_saved_1688_session() -> bool:
    return get_storage_state_path().exists()
