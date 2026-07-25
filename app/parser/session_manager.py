from __future__ import annotations

from pathlib import Path


def storage_state_exists(path: str) -> bool:
    return Path(path).exists()
