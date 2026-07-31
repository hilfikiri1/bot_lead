"""Regression tests for production startup and webhook safety."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app import main as app_main
from app.api import telegram as telegram_api
from app.services import goals_qa_service


def test_drive_upload_failure_is_high_priority() -> None:
    text = "Google Drive не загружает файл и показывает ошибку"
    assert goals_qa_service.infer_priority(text) == "High"


def test_telegram_webhook_fails_closed_in_production_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_api.settings, "app_env", "production")
    monkeypatch.setattr(telegram_api.settings, "telegram_webhook_secret", "")
    assert telegram_api._verify_secret(None) is False
    assert telegram_api._verify_secret("forged") is False


def test_telegram_webhook_requires_exact_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_api.settings, "app_env", "production")
    monkeypatch.setattr(telegram_api.settings, "telegram_webhook_secret", "expected")
    assert telegram_api._verify_secret("expected") is True
    assert telegram_api._verify_secret("wrong") is False


def test_development_webhook_fixture_can_run_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_api.settings, "app_env", "development")
    monkeypatch.setattr(telegram_api.settings, "telegram_webhook_secret", "")
    assert telegram_api._verify_secret(None) is True


@pytest.mark.asyncio
async def test_lifespan_aborts_when_database_migration_fails() -> None:
    with patch.object(
        app_main,
        "upgrade_database",
        new=AsyncMock(side_effect=RuntimeError("migration failed")),
    ):
        with pytest.raises(RuntimeError, match="migration failed"):
            async with app_main.lifespan(app_main.app):
                pass


class _HealthyConnection:
    async def execute(self, statement):
        assert "SELECT 1" in str(statement)


class _ConnectionContext:
    def __init__(self, *, error: Exception | None = None):
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return _HealthyConnection()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, *, error: Exception | None = None):
        self.error = error

    def connect(self):
        return _ConnectionContext(error=self.error)


@pytest.mark.asyncio
async def test_ready_checks_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main, "engine", _FakeEngine())
    result = await app_main.ready()
    assert result["status"] == "ready"


@pytest.mark.asyncio
async def test_ready_returns_503_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_main,
        "engine",
        _FakeEngine(error=RuntimeError("database unavailable")),
    )
    with pytest.raises(HTTPException) as exc_info:
        await app_main.ready()
    assert exc_info.value.status_code == 503
