"""Manage the persisted Playwright storage state (1688 login session).

The storage state is created manually by an administrator via
``scripts/login_1688.py`` and re-used by the bot when opening product pages.
Secrets/cookies live only on disk (a volume) — never in the database.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.logging_config import get_logger

logger = get_logger(__name__)


class SessionManager:
    """Loads and validates the 1688 Playwright storage-state file."""

    def __init__(self, storage_state_path: Path):
        self._path = storage_state_path

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists() and self._path.stat().st_size > 0

    def storage_state_arg(self) -> str | None:
        """Return the path to pass to Playwright's ``storage_state`` argument.

        Returns ``None`` when no valid session file exists so the browser can
        still open (pages may then require login, which is handled downstream).
        """
        if not self.exists():
            logger.warning(
                "1688 storage state not found; opening without a saved session",
                path=str(self._path),
            )
            return None

        try:
            with self._path.open("r", encoding="utf-8") as handle:
                json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Invalid storage state file; ignoring", error=str(exc))
            return None

        return str(self._path)
