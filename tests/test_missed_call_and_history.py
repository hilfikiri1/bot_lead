"""Missed-call follow-up + richer project history / conversation button."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.agent import project_snapshot
from app.services import missed_call_followup_service


def test_project_actions_include_conversation_and_history():
    snapshot = project_snapshot.ProjectSnapshot(
        identity={"kommo_lead_id": 77},
        kommo={"url": "https://kommo.test/77"},
    )
    markup = project_snapshot.project_actions_markup(snapshot)
    labels = {
        button["text"]
        for row in markup["inline_keyboard"]
        for button in row
    }
    assert "💬 Переписка" in labels
    assert "🕘 История" in labels
    callbacks = {
        button.get("callback_data")
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    }
    assert "agent:comms:77:0" in callbacks
    assert "agent:project:history:77" in callbacks


def test_is_missed_call_status_detects_poland_stage():
    assert missed_call_followup_service.is_missed_call_status("НЕДОЗВОН")
    assert missed_call_followup_service.is_missed_call_status("Недозвон")
    assert not missed_call_followup_service.is_missed_call_status("Первый контакт")


def test_count_missed_attempts_prefers_markers():
    day = "2026-07-31"
    notes = [
        {"text": f"Недозвон\n[BBS-MISSED-CALL:{day}:1]", "created_at": 1785480000},
        {"text": "Результат звонка: Не ответил", "created_at": 1785481000},
    ]
    assert missed_call_followup_service.count_missed_attempts_today(notes, day=day) == 1


def test_humanize_history_note():
    assert "Первичный анализ" in project_snapshot._humanize_history_note(
        "[BBS-ONBOARD-169-1] ПЕРВИЧНЫЙ АНАЛИЗ НОВОГО ЛИДА ..."
    )
    assert "Недозвон" in project_snapshot._humanize_history_note(
        "Недозвон · попытка 1 [BBS-MISSED-CALL:2026-07-31:1]"
    )


@pytest.mark.asyncio
async def test_first_missed_call_creates_today_task():
    notes: list[dict] = []
    tasks: list[dict] = []

    async def add_note(lead_id, text):
        notes.append({"text": text, "created_at": int(datetime.now().timestamp())})
        return True

    async def create_task(*, lead_id, text, complete_till, responsible_user_id=None):
        tasks.append({"text": text, "complete_till": complete_till})
        return {"task_id": 11, "text": text, "complete_till": complete_till}

    with (
        patch(
            "app.services.missed_call_followup_service.kommo_service.get_recent_common_notes",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.missed_call_followup_service.kommo_service.get_open_lead_tasks",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.missed_call_followup_service.kommo_service.add_common_note",
            new=add_note,
        ),
        patch(
            "app.services.missed_call_followup_service.kommo_service.create_lead_task",
            new=create_task,
        ),
    ):
        result = await missed_call_followup_service.handle_missed_call(169, source="test")

    assert result["attempt"] == 1
    assert result["due_rule"] == "today"
    assert result["task_created"] is True
    assert "сегодня" in tasks[0]["text"].casefold() or "сегодня" in notes[0]["text"].casefold()
    assert "[BBS-MISSED-CALL:" in notes[0]["text"]


@pytest.mark.asyncio
async def test_second_missed_call_creates_tomorrow_task():
    day = datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m-%d")
    existing = [
        {
            "text": f"Недозвон · попытка 1\n[BBS-MISSED-CALL:{day}:1]",
            "created_at": int(datetime.now().timestamp()),
        }
    ]
    created: list[dict] = []

    async def create_task(*, lead_id, text, complete_till, responsible_user_id=None):
        created.append({"text": text, "complete_till": complete_till})
        return {"task_id": 22, "text": text}

    with (
        patch(
            "app.services.missed_call_followup_service.kommo_service.get_recent_common_notes",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "app.services.missed_call_followup_service.kommo_service.get_open_lead_tasks",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.missed_call_followup_service.kommo_service.add_common_note",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.missed_call_followup_service.kommo_service.create_lead_task",
            new=create_task,
        ),
    ):
        result = await missed_call_followup_service.handle_missed_call(169)

    assert result["attempt"] == 2
    assert result["due_rule"] == "tomorrow"
    assert "завтра" in created[0]["text"].casefold()
    due = datetime.fromtimestamp(created[0]["complete_till"], tz=ZoneInfo("Europe/Warsaw"))
    assert due.date() >= (datetime.now(ZoneInfo("Europe/Warsaw")) + timedelta(days=1)).date() or due.weekday() < 5


@pytest.mark.asyncio
async def test_build_history_shows_operational_events_not_raw_ops():
    lead = {
        "id": 169,
        "name": "169 - Сельскохозяйственные товары",
        "status_name": "НЕДОЗВОН",
        "notes": [],
    }
    notes = [
        {
            "text": "[BBS-ONBOARD-169-1] ПЕРВИЧНЫЙ АНАЛИЗ НОВОГО ЛИДА Внутренний номер...",
            "created_at": 1785470000,
        },
        {
            "text": "Недозвон · попытка 1\n[BBS-MISSED-CALL:2026-07-31:1]",
            "created_at": 1785474000,
        },
    ]
    tasks = [
        {
            "text": "Позвонить еще раз сегодня [BBS-MISSED-CALL:2026-07-31:1]",
            "complete_till": 1785480000,
        }
    ]

    class _DB:
        async def execute(self, *_args, **_kwargs):
            class _R:
                def scalars(self):
                    class _S:
                        def all(self):
                            return []

                    return _S()

            return _R()

    with (
        patch(
            "app.agent.project_snapshot.kommo_service.get_recent_common_notes",
            new=AsyncMock(return_value=notes),
        ),
        patch(
            "app.agent.project_snapshot.kommo_service.get_open_lead_tasks",
            new=AsyncMock(return_value=tasks),
        ),
        patch(
            "app.agent.project_snapshot.project_artifact_service.recent_for_project",
            new=AsyncMock(return_value=[]),
        ),
    ):
        text = await project_snapshot.build_history(_DB(), lead=lead)

    assert "НЕДОЗВОН" in text
    assert "Первичный анализ" in text
    assert "Недозвон" in text
    assert "Задача" in text
    assert "update_kommo_lead" not in text
