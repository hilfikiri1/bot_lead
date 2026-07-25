"""File cleanup service."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings, get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class CleanupService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def cleanup_job(self, job_id: uuid.UUID, *, keep_pdf_hours: int | None = None) -> None:
        job_dir = self.settings.storage_temporary / str(job_id)
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            logger.info("job_temp_cleaned", job_id=str(job_id))

    async def cleanup_expired_files(self) -> int:
        """Remove expired PDFs from output directory."""
        retention = timedelta(hours=self.settings.pdf_retention_hours)
        cutoff = datetime.now(timezone.utc) - retention
        removed = 0

        output_dir = self.settings.storage_output
        if not output_dir.exists():
            return 0

        for path in output_dir.glob("*.pdf"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except Exception as exc:
                logger.warning("cleanup_failed", path=str(path), error=str(exc))

        expired_dirs = self._find_expired_temp_dirs(cutoff)
        for d in expired_dirs:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1

        logger.info("cleanup_completed", removed=removed)
        return removed

    def _find_expired_temp_dirs(self, cutoff: datetime) -> list[Path]:
        temp_dir = self.settings.storage_temporary
        if not temp_dir.exists():
            return []
        result: list[Path] = []
        for child in temp_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = datetime.fromtimestamp(child.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    result.append(child)
            except Exception:
                continue
        return result
