from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.agent import service as agent_service
from app.agent.contracts import AgentReply
from app.services import personal_journal_runtime


@pytest.mark.asyncio
async def test_personal_mode_keeps_weekly_words_as_diary(monkeypatch):
    fallback = AsyncMock(return_value=AgentReply("weekly", intent="weekly_review"))
    session = SimpleNamespace(context={"pending_daily_reflection": {"scope": "personal"}})
    save = AsyncMock(return_value=(SimpleNamespace(id=44), {"id": "entry-1", "notion_status": "ok", "notion_url": "https://notion.test/personal"}))
    monkeypatch.setattr(personal_journal_runtime, "_INSTALLED", False)
    monkeypatch.setattr(agent_service, "handle_message", fallback)
    monkeypatch.setattr(personal_journal_runtime.memory, "get_or_create_session", AsyncMock(return_value=session))
    monkeypatch.setattr(personal_journal_runtime, "save_personal_transcript", save)
    personal_journal_runtime.install_personal_journal_runtime()

    reply = await agent_service.handle_message(
        AsyncMock(), chat_id=20, telegram_user_id=10,
        text="Итоги недели для меня такие: хочу изменить свой режим.", source="voice"
    )

    assert reply.intent == "personal_journal_saved"
    assert "сохранена в Notion" in reply.text
    save.assert_awaited_once()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_mode_allows_explicit_slash_command(monkeypatch):
    fallback = AsyncMock(return_value=AgentReply("week", intent="weekly_review"))
    session = SimpleNamespace(context={"pending_daily_reflection": {"scope": "personal"}})
    save = AsyncMock()
    monkeypatch.setattr(personal_journal_runtime, "_INSTALLED", False)
    monkeypatch.setattr(agent_service, "handle_message", fallback)
    monkeypatch.setattr(personal_journal_runtime.memory, "get_or_create_session", AsyncMock(return_value=session))
    monkeypatch.setattr(personal_journal_runtime, "save_personal_transcript", save)
    personal_journal_runtime.install_personal_journal_runtime()

    reply = await agent_service.handle_message(
        AsyncMock(), chat_id=20, telegram_user_id=10, text="/week", source="text"
    )

    assert reply.intent == "weekly_review"
    fallback.assert_awaited_once()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_notion_personal_entry_is_plain_transcript(monkeypatch):
    request = AsyncMock(side_effect=[{"results": [], "has_more": False}, {"id": "page"}])
    monkeypatch.setenv("NOTION_PERSONAL_JOURNAL_PAGE_ID", "page-id")
    monkeypatch.setattr(personal_journal_runtime.notion_gateway, "_request", request)
    monkeypatch.setattr(personal_journal_runtime.notion_gateway, "notion_page_url", lambda page_id: f"https://notion.test/{page_id}")

    status, _ = await personal_journal_runtime._append_to_notion(
        marker="abc123",
        raw="Это моя личная запись.",
        recorded_at=datetime(2026, 8, 13, 20, 14, tzinfo=ZoneInfo("Europe/Warsaw")),
    )

    assert status == "ok"
    children = request.await_args_list[1].kwargs["json"]["children"]
    rendered = str(children)
    assert "13.08.2026 · 20:14" in rendered
    assert "Это моя личная запись." in rendered
    assert "Как ИИ структурировал" not in rendered


@pytest.mark.asyncio
async def test_save_uses_only_daily_personal_entry(monkeypatch):
    entry = SimpleNamespace(id=7, analysis={}, raw_text=None, status="pending", remind_at=None, source=None)
    get_entry = AsyncMock(return_value=entry)
    clear = AsyncMock()
    db = AsyncMock()
    session = SimpleNamespace(context={"pending_daily_reflection": {"scope": "personal"}})
    monkeypatch.setattr(personal_journal_runtime.kaizen_journal_service, "get_or_create_entry", get_entry)
    monkeypatch.setattr(personal_journal_runtime.kaizen_journal_service, "clear_pending_reflection", clear)
    monkeypatch.setattr(personal_journal_runtime.kaizen_journal_service, "local_now", lambda: datetime(2026, 8, 13, 20, 14, tzinfo=ZoneInfo("Europe/Warsaw")))
    monkeypatch.setattr(personal_journal_runtime, "_append_to_notion", AsyncMock(return_value=("ok", "https://notion.test/personal")))

    _, saved = await personal_journal_runtime.save_personal_transcript(
        db, telegram_user_id=10, session=session, text="Моя личная запись.", source="voice"
    )

    assert get_entry.await_args.kwargs["entry_type"] == "daily_personal"
    assert "[20:14] Моя личная запись." in entry.raw_text
    assert "personal_transcript_v1" in entry.analysis
    clear.assert_awaited_once()
    assert saved["notion_status"] == "ok"
