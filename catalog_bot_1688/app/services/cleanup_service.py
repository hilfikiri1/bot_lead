"""Background cleanup of expired PDFs and stale temporary folders."""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.database.repositories import CatalogJobRepository
from app.database.session import session_scope
from app.logging_config import get_logger

logger = get_logger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600
TEMP_MAX_AGE_SECONDS = 6 * 3600


class CleanupService:
    """Periodically deletes output PDFs older than the retention window and any
    orphaned temporary job folders."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        logger.info(
            "Cleanup service started",
            retention_hours=self._settings.pdf_retention_hours,
        )
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - never crash the loop
                logger.warning("Cleanup iteration failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CLEANUP_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> int:
        removed = 0
        removed += self._clean_temp_dirs()
        removed += await self._clean_expired_pdfs()
        if removed:
            logger.info("Cleanup removed items", count=removed)
        return removed

    def _clean_temp_dirs(self) -> int:
        removed = 0
        temp_root = self._settings.temporary_path
        if not temp_root.exists():
            return 0
        now = time.time()
        for entry in temp_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age > TEMP_MAX_AGE_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    async def _clean_expired_pdfs(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(
            hours=self._settings.pdf_retention_hours
        )
        removed = 0
        async with session_scope() as session:
            repo = CatalogJobRepository(session)
            expired = await repo.find_expired(cutoff)
            for job in expired:
                if job.output_file:
                    path = Path(job.output_file)
                    if path.exists():
                        try:
                            path.unlink()
                            removed += 1
                        except OSError:
                            continue
                    await repo.clear_output_file(job.id)
        return removed
