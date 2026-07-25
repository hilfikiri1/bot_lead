from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import shutil

from app.config import get_settings


class CleanupService:
    def __init__(self) -> None:
        self._settings = get_settings()

    def cleanup_job_temp(self, job_dir: Path) -> None:
        if job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

    def cleanup_old_output_files(self) -> None:
        output_dir = Path(self._settings.storage_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        threshold = datetime.utcnow() - timedelta(hours=self._settings.pdf_retention_hours)
        for path in output_dir.glob("*.pdf"):
            modified = datetime.utcfromtimestamp(path.stat().st_mtime)
            if modified < threshold:
                path.unlink(missing_ok=True)
