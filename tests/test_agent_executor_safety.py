from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.action_utils import action_key
from app.agent.executor import execute_action
from app.agent.security import sanitize_text


def test_action_key_is_stable_across_time():
    payload = {"lead_id": 42, "note_text": "клиент ждёт цену"}
    first = action_key(
        telegram_user_id=7, action_type="add_kommo_note", payload=payload
    )
    second = action_key(
        telegram_user_id=7, action_type="add_kommo_note", payload=payload
    )
    assert first == second
    assert len(first) == 64


def test_callback_error_path_sanitizes_secrets():
    source = open("app/api/telegram.py", encoding="utf-8").read()
    assert "sanitize_text(str(exc)" in source
    assert 'f"❌ Ошибка выполнения действия: {html.escape(str(exc)[:500])}"' not in source


def test_confirmed_notion_sync_uses_force_flag():
    source = open("app/agent/executor.py", encoding="utf-8").read()
    assert "force=True" in source
    notion_source = open("app/services/notion_service.py", encoding="utf-8").read()
    assert "force: bool = False" in notion_source
    assert "not force and not settings.notion_auto_sync" in notion_source


def test_telegram_update_claim_exists():
    source = open("app/api/telegram.py", encoding="utf-8").read()
    assert "async def _claim_telegram_update" in source
    assert "Duplicate Telegram update ignored" in source


@pytest.mark.asyncio
async def test_execute_action_is_idempotent_after_success():
    action = SimpleNamespace(
        id=11,
        telegram_user_id=99,
        status="executed",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        approved_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        payload={},
        action_type="add_kommo_note",
        result={"ok": True},
        error_message=None,
        executed_at=datetime.now(timezone.utc),
    )
    locked = MagicMock()
    locked.scalar_one_or_none.return_value = action
    db = AsyncMock()
    db.execute = AsyncMock(return_value=locked)

    message = await execute_action(db, action=action, telegram_user_id=99)
    assert "уже было выполнено" in message


@pytest.mark.asyncio
async def test_stale_executing_action_is_marked_failed():
    action = SimpleNamespace(
        id=12,
        telegram_user_id=99,
        status="executing",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        approved_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        payload={},
        action_type="add_kommo_note",
        result=None,
        error_message=None,
        executed_at=None,
    )
    locked = MagicMock()
    locked.scalar_one_or_none.return_value = action
    db = AsyncMock()
    db.execute = AsyncMock(return_value=locked)
    db.commit = AsyncMock()

    message = await execute_action(db, action=action, telegram_user_id=99)
    assert action.status == "failed"
    assert "зависло" in message
    assert "Bearer abc" not in (action.error_message or "")


def test_sanitize_keeps_working_without_secrets():
    assert sanitize_text("обычная ошибка Kommo 400") == "обычная ошибка Kommo 400"
    redacted = sanitize_text("Authorization: Bearer super.secret.token api_key=abc")
    assert "super.secret.token" not in redacted
    assert "abc" not in redacted
