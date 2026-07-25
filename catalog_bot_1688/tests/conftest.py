"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project importable when running `pytest` from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="test-token",
        OPENAI_API_KEY="test-key",
        OPENAI_MODEL="gpt-5-mini",
        DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/test",
    )


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
