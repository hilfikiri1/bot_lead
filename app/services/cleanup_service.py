from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings


class CleanupService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def cleanup_job_temporary(self, job_dir: Path, *, keep_pdf: bool = True) -> None:
        if keep_pdf:
            for child in job_dir.iterdir() if job_dir.exists() else []:
                if child.suffix.lower() != ".pdf":
                    shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
        else:
            shutil.rmtree(job_dir, ignore_errors=True)

    async def cleanup_expired_outputs(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.pdf_retention_hours)
        removed = 0
        for directory in (self.settings.output_dir, self.settings.temporary_dir):
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if path.is_file():
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    if mtime < cutoff:
                        path.unlink(missing_ok=True)
                        removed += 1
        return removed
