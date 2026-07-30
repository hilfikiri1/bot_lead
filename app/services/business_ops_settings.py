from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BusinessOpsSettings:
    notion_goals_data_source_id: str
    notion_qa_data_source_id: str
    google_drive_qa_folder_id: str
    qa_notion_sync_enabled: bool
    qa_drive_upload_enabled: bool
    goals_notion_sync_enabled: bool


def get_business_ops_settings() -> BusinessOpsSettings:
    """Read optional rollout settings without changing the core Settings contract."""

    return BusinessOpsSettings(
        notion_goals_data_source_id=_env(
            "NOTION_GOALS_DATA_SOURCE_ID",
            "72eafee2-9418-4730-89f0-1dd24cff6873",
        ),
        notion_qa_data_source_id=_env(
            "NOTION_QA_DATA_SOURCE_ID",
            "7d481e62-492c-4510-9396-8fbe5d10d3f8",
        ),
        google_drive_qa_folder_id=_env("GOOGLE_DRIVE_QA_FOLDER_ID"),
        qa_notion_sync_enabled=_bool("QA_NOTION_SYNC_ENABLED", False),
        qa_drive_upload_enabled=_bool("QA_DRIVE_UPLOAD_ENABLED", False),
        goals_notion_sync_enabled=_bool("GOALS_NOTION_SYNC_ENABLED", True),
    )
