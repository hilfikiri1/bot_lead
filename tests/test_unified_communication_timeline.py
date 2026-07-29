from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import (
    communication_timeline_runtime,
    unified_communication_service,
)


def _entry(
    *,
    entry_id: str,
    occurred_at: datetime,
    direction: str,
    text: str,
    channel: str = "Facebook",
    external_id: str | None = None,
):
    return unified_communication_service.CommunicationEntry(
        id=entry_id,
        occurred_at=occurred_at,
        channel=channel,
        direction=direction,
        text=text,
        external_id=external_id,
    )


def test_analysis_detects_waiting_on_us_and_overdue_manager_promise():
    made_at = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry(
            entry_id="out-1",
            occurred_at=made_at,
            direction="outgoing",
            text="Jutro na pewno się z Panią skontaktuję.",
        ),
        _entry(
            entry_id="in-1",
            occurred_at=made_at + timedelta(hours=17),
            direction="incoming",
            text="Witam, dziękuję i czekam. Czy zadzwoni Pan dzisiaj?",
        ),
    ]

    result = unified_communication_service.analyze_entries(entries, now=now)

    assert result.waiting_on == "us"
    assert result.last_direction == "incoming"
    assert "czekam" in str(result.last_client_message).casefold()
    assert result.promises_by_us
    assert result.promises_by_us[0].due_at is not None
    assert result.overdue_promises
    assert "просроч" in str(result.recommended_action).casefold()
    assert result.open_questions


def test_analysis_waits_for_client_after_last_outgoing_message():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry(
            entry_id="in",
            occurred_at=now - timedelta(hours=2),
            direction="incoming",
            text="Proszę przesłać ofertę.",
        ),
        _entry(
            entry_id="out",
            occurred_at=now - timedelta(hours=1),
            direction="outgoing",
            text="Dziękuję. Prześlemy ofertę po sprawdzeniu specyfikacji.",
        ),
    ]
    result = unified_communication_service.analyze_entries(entries, now=now)
    assert result.waiting_on == "client"
    assert result.last_direction == "outgoing"
    assert result.client_requests == ["Proszę przesłać ofertę."]
    assert result.promises_by_us


def test_deduplicate_removes_same_message_from_note_and_draft():
    when = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry(
            entry_id="draft",
            occurred_at=when,
            direction="outgoing",
            text="Dzień dobry, proszę o odpowiedź.",
            channel="WhatsApp",
        ),
        _entry(
            entry_id="note",
            occurred_at=when + timedelta(seconds=10),
            direction="outgoing",
            text="Dzień dobry, proszę o odpowiedź.",
            channel="WhatsApp",
        ),
    ]
    result = unified_communication_service._deduplicate(entries)
    assert len(result) == 1


def test_format_timeline_shows_channel_actor_promise_and_recommendation():
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    entries = [
        _entry(
            entry_id="out",
            occurred_at=now - timedelta(days=2),
            direction="outgoing",
            text="Jutro zadzwonię po 14:00.",
            channel="Facebook",
        ),
        _entry(
            entry_id="in",
            occurred_at=now - timedelta(days=1),
            direction="incoming",
            text="Dziękuję, czekam.",
            channel="Facebook",
        ),
    ]
    result = unified_communication_service.UnifiedCommunicationResult(
        lead_id=118,
        entries=entries,
        analysis=unified_communication_service.analyze_entries(entries, now=now),
    )
    text = unified_communication_service.format_timeline(
        result, lead_name="118 - Карнизы"
    )
    assert "Facebook" in text
    assert "клиент" in text
    assert "Наше последнее обещание" in text
    assert "Просроченных обещаний" in text
    assert "Рекомендация" in text


def test_timeline_button_is_added_before_external_link():
    original = {
        "inline_keyboard": [
            [{"text": "Task", "callback_data": "agent:task"}],
            [{"text": "Kommo", "url": "https://example.kommo.com"}],
        ]
    }
    result = communication_timeline_runtime._add_timeline_button(original, 118)
    assert result["inline_keyboard"][-2][0]["callback_data"] == "agent:comms:118:0"
    assert result["inline_keyboard"][-1][0]["url"]


@pytest.mark.asyncio
async def test_build_unified_timeline_keeps_other_sources_when_chat_scope_missing():
    db = AsyncMock()
    draft_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db.execute = AsyncMock(return_value=draft_result)
    note_entry = _entry(
        entry_id="note",
        occurred_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        direction="internal",
        text="Внутренняя заметка",
        channel="Примечание Kommo",
    )

    with (
        patch.object(
            unified_communication_service,
            "_chat_entries",
            AsyncMock(return_value=([], "Kommo: нет разрешения External chat history")),
        ),
        patch.object(
            unified_communication_service,
            "_note_entries",
            AsyncMock(return_value=([note_entry], None)),
        ),
        patch.object(
            unified_communication_service,
            "_draft_entries",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            unified_communication_service,
            "_project_event_entries",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await unified_communication_service.build_unified_timeline(
            db, lead_id=118
        )

    assert result.entries == [note_entry]
    assert result.source_errors == ["Kommo: нет разрешения External chat history"]
    assert result.analysis.summary == "Коммуникации по проекту не найдены."


def test_timeline_navigation_callbacks_fit_telegram_limit():
    markup = unified_communication_service.timeline_markup(
        lead_id=15011973,
        offset=10,
        total=50,
        lead_url="https://example.kommo.com/leads/detail/15011973",
    )
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
