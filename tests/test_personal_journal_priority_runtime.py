from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent import service as agent_service
from app.agent.contracts import AgentReply
from app.services import personal_journal_priority_runtime


@pytest.mark.asyncio
async def test_personal_voice_bypasses_stale_bug_intake(monkeypatch):
    session = SimpleNamespace(
        context={
            "pending_daily_reflection": {"scope": "personal"},
            "qa_intake": {"pending_screenshot_issue_id": 7},
            "active_qa_issue_id": 7,
        }
    )
    underlying = AsyncMock(return_value=AgentReply("BUG-0007", intent="bug_saved"))
    save = AsyncMock(
        return_value=(
            SimpleNamespace(id=44),
            {
                "id": "personal-1",
                "notion_status": "ok",
                "notion_url": "https://notion.test/personal",
            },
        )
    )
    update_context = AsyncMock()

    monkeypatch.setattr(personal_journal_priority_runtime, "_INSTALLED", False)
    monkeypatch.setattr(agent_service, "handle_message", underlying)
    monkeypatch.setattr(
        personal_journal_priority_runtime.memory,
        "get_or_create_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(
        personal_journal_priority_runtime.memory,
        "update_context",
        update_context,
    )
    monkeypatch.setattr(
        personal_journal_priority_runtime.personal_journal_runtime,
        "save_personal_transcript",
        save,
    )

    personal_journal_priority_runtime.install_personal_journal_priority_runtime()
    reply = await agent_service.handle_message(
        AsyncMock(),
        chat_id=20,
        telegram_user_id=10,
        text="Это моя личная запись.",
        source="voice",
    )

    assert reply.intent == "personal_journal_saved"
    assert "сохранена в Notion" in reply.text
    save.assert_awaited_once()
    underlying.assert_not_awaited()
    update_context.assert_awaited_once()
    assert update_context.await_args.kwargs["values"] == {
        "qa_intake": None,
        "active_qa_issue_id": None,
    }


@pytest.mark.asyncio
async def test_personal_button_clears_stale_bug_context(monkeypatch):
    session = SimpleNamespace(
        context={
            "pending_daily_reflection": {"scope": "personal"},
            "qa_intake": {"pending_screenshot_issue_id": 7},
            "active_qa_issue_id": 7,
        }
    )
    callback = AsyncMock(
        return_value=AgentReply("Личный дневник", intent="daily_reflection_personal_waiting")
    )
    update_context = AsyncMock()

    monkeypatch.setattr(personal_journal_priority_runtime, "_INSTALLED", False)
    monkeypatch.setattr(agent_service, "handle_callback", callback)
    monkeypatch.setattr(
        personal_journal_priority_runtime.memory,
        "get_or_create_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(
        personal_journal_priority_runtime.memory,
        "update_context",
        update_context,
    )

    personal_journal_priority_runtime.install_personal_journal_priority_runtime()
    reply = await agent_service.handle_callback(
        AsyncMock(),
        callback_data="agent:reflection:personal:2026-08-13",
        telegram_user_id=10,
        chat_id=20,
    )

    assert reply.intent == "daily_reflection_personal_waiting"
    callback.assert_awaited_once()
    update_context.assert_awaited_once()
    assert update_context.await_args.kwargs["values"] == {
        "qa_intake": None,
        "active_qa_issue_id": None,
    }
