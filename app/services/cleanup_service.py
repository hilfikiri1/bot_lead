from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL_SECONDS = 3600  # Run every hour


class CleanupService:
    """Periodically removes old output PDFs and orphaned temp directories."""

    async def run_periodic(self) -> None:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                await self.cleanup_old_files()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("cleanup_error", error=str(exc))

    async def cleanup_old_files(self) -> None:
        retention_secs = settings.pdf_retention_hours * 3600
        now = time.time()
        removed = 0

        # Clean output PDFs
        output_dir = Path(settings.output_storage_dir)
        if output_dir.exists():
            for pdf in output_dir.glob("*.pdf"):
                try:
                    if now - pdf.stat().st_mtime > retention_secs:
                        pdf.unlink()
                        removed += 1
                except Exception as exc:
                    logger.warning("cleanup_file_error", path=str(pdf), error=str(exc))

        # Clean orphaned temp job dirs older than 2 × retention
        temp_dir = Path(settings.temp_storage_dir)
        if temp_dir.exists():
            for job_dir in temp_dir.iterdir():
                if not job_dir.is_dir():
                    continue
                try:
                    if now - job_dir.stat().st_mtime > retention_secs * 2:
                        shutil.rmtree(job_dir)
                        removed += 1
                except Exception as exc:
                    logger.warning(
                        "cleanup_dir_error", path=str(job_dir), error=str(exc)
                    )

        if removed:
            logger.info("cleanup_completed", removed=removed)
