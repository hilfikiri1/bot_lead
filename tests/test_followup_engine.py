from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import (
    followup_service,
    next_action_service,
    whatsapp_webhook_service,
)


def _draft() -> SimpleNamespace:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=17,
        kommo_lead_id=118,
        status="sent",
        delivery_marker="BBS-MSG-test",
        sent_at=now,
        sent_confirmed_at=now,
        updated_at=now,
        channel="whatsapp",
        recipient="48660000000",
        client_name="Ewa Test",
        body="Dzień dobry, proszę o odpowiedź.",
        metadata_json={},
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        kommo_lead_id=118,
        status="missing",
        waiting_on=None,
        action_text=None,
        due_at=None,
        responsible_user_id=None,
        last_contact_at=None,
        stale_reason=None,
        metadata_json={},
    )


def test_followup_prompt_has_all_choices_and_safe_callback_lengths():
    markup = followup_service.followup_prompt_markup(123456)
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    labels = {button["text"] for button in buttons}
    assert {"Завтра", "Через 3 дня", "Через 7 дней", "Выбрать дату", "Не напоминать"} <= labels
    assert all(len(button["callback_data"].encode("utf-8")) <= 64 for button in buttons)


def test_preset_and_custom_dates_use_manager_timezone(monkeypatch):
    monkeypatch.setattr(followup_service.settings, "manager_timezone", "Europe/Warsaw")
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    tomorrow = followup_service.preset_due_at("tomorrow", now=now)
    assert tomorrow.astimezone(followup_service._manager_tz()).strftime("%d.%m.%Y %H:%M") == "30.07.2026 10:00"
    custom = followup_service.parse_custom_due_at("31.07.2026 15:30", now=now)
    assert custom.astimezone(followup_service._manager_tz()).strftime("%d.%m.%Y %H:%M") == "31.07.2026 15:30"


@pytest.mark.asyncio
async def test_schedule_from_sent_draft_persists_waiting_and_creates_one_kommo_task():
    db = AsyncMock()
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = SimpleNamespace(id=1, telegram_user_id=100)
    db.execute = AsyncMock(return_value=user_result)
    db.commit = AsyncMock()
    row = _state()
    due_at = datetime.now(timezone.utc) + timedelta(days=3)

    with (
        patch.object(
            followup_service.client_message_service,
            "get_draft",
            AsyncMock(return_value=_draft()),
        ),
        patch.object(
            followup_service.kommo_service,
            "get_lead_details",
            AsyncMock(
                return_value={
                    "id": 118,
                    "name": "118 - Карнизы",
                    "responsible_user_id": 77,
                    "url": "https://example.kommo.com/leads/detail/118",
                }
            ),
        ),
        patch.object(
            followup_service,
            "_state_for_update",
            AsyncMock(return_value=row),
        ),
        patch.object(
            followup_service.kommo_service,
            "get_open_lead_tasks",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            followup_service.kommo_service,
            "create_lead_task",
            AsyncMock(return_value={"task_id": 991}),
        ) as create_task,
    ):
        result = await followup_service.schedule_from_draft(
            db,
            draft_id=17,
            telegram_user_id=100,
            due_at=due_at,
            preset="3d",
        )

    assert row.waiting_on == "client"
    assert row.status == "waiting_client"
    assert row.due_at == due_at
    assert row.metadata_json["followup"]["status"] == "scheduled"
    assert row.metadata_json["followup"]["kommo_task_id"] == 991
    assert result["kommo_task_id"] == 991
    create_task.assert_awaited_once()
    assert "[BBS-FOLLOWUP-17]" in create_task.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_schedule_reuses_existing_marker_task():
    db = AsyncMock()
    user_result = MagicMock()
    user_result.scalar_one_or_none.return_value = SimpleNamespace(id=1, telegram_user_id=100)
    db.execute = AsyncMock(return_value=user_result)
    db.commit = AsyncMock()
    row = _state()
    due_at = datetime.now(timezone.utc) + timedelta(days=1)

    with (
        patch.object(followup_service.client_message_service, "get_draft", AsyncMock(return_value=_draft())),
        patch.object(
            followup_service.kommo_service,
            "get_lead_details",
            AsyncMock(return_value={"id": 118, "name": "118 - Карнизы", "responsible_user_id": 77}),
        ),
        patch.object(followup_service, "_state_for_update", AsyncMock(return_value=row)),
        patch.object(
            followup_service.kommo_service,
            "get_open_lead_tasks",
            AsyncMock(return_value=[{"id": 444, "text": "Проверить · [BBS-FOLLOWUP-17]"}]),
        ),
        patch.object(
            followup_service.kommo_service,
            "create_lead_task",
            AsyncMock(),
        ) as create_task,
    ):
        result = await followup_service.schedule_from_draft(
            db,
            draft_id=17,
            telegram_user_id=100,
            due_at=due_at,
        )

    assert result["kommo_task_id"] == 444
    create_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_incoming_reply_closes_waiting_state_and_completes_task():
    row = _state()
    row.waiting_on = "client"
    row.status = "waiting_client"
    row.due_at = datetime.now(timezone.utc) + timedelta(days=1)
    row.metadata_json = {
        "followup": {
            "status": "scheduled",
            "sent_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "kommo_task_id": 555,
        }
    }
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    incoming_at = datetime.now(timezone.utc)

    with patch.object(
        followup_service,
        "_complete_kommo_task",
        AsyncMock(),
    ) as complete_task:
        updated = await followup_service.close_followup(
            db,
            lead_id=118,
            reason="incoming_whatsapp",
            waiting_on="us",
            action_text="Ответить клиенту",
            incoming_at=incoming_at,
            incoming_message_id="wamid-1",
        )

    assert updated is row
    assert row.waiting_on == "us"
    assert row.status == "waiting_us"
    assert row.due_at is None
    assert row.metadata_json["followup"]["status"] == "closed"
    assert row.metadata_json["followup"]["closed_reason"] == "incoming_whatsapp"
    complete_task.assert_awaited_once_with(555, result_text="Follow-up closed: incoming_whatsapp")


def test_next_action_uses_persisted_waiting_state_and_due_date():
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    view = next_action_service.evaluate_lead_next_action(
        {
            "id": 118,
            "name": "118 - Карнизы",
            "updated_at": int(now.timestamp()),
            "closest_task_at": None,
        },
        now=now,
        waiting_on="client",
        action_text="Проверить ответ клиента",
        stored_due_at=now + timedelta(days=2),
    )
    assert view.category == "waiting_client"
    assert view.status == "waiting_client"
    assert view.due_at == now + timedelta(days=2)

    overdue = next_action_service.evaluate_lead_next_action(
        {
            "id": 118,
            "name": "118 - Карнизы",
            "updated_at": int(now.timestamp()),
            "closest_task_at": None,
        },
        now=now,
        waiting_on="client",
        action_text="Проверить ответ клиента",
        stored_due_at=now - timedelta(hours=1),
    )
    assert overdue.category == "overdue"
    assert "follow-up" in str(overdue.stale_reason)


@pytest.mark.asyncio
async def test_whatsapp_incoming_reconciles_active_followup(monkeypatch):
    close = AsyncMock(
        return_value=SimpleNamespace(
            metadata_json={
                "followup": {
                    "closed_reason": "incoming_whatsapp",
                    "incoming_message_id": "wamid-2",
                }
            }
        )
    )
    monkeypatch.setattr(whatsapp_webhook_service.followup_service, "enabled", lambda: True)
    monkeypatch.setattr(whatsapp_webhook_service.followup_service, "close_followup", close)

    fake_db = AsyncMock()

    class SessionContext:
        async def __aenter__(self):
            return fake_db

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(whatsapp_webhook_service, "AsyncSessionLocal", lambda: SessionContext())
    result = await whatsapp_webhook_service._close_active_followup(
        {"id": 118},
        {"message_id": "wamid-2", "timestamp": 1785326400, "text": "Dziękuję"},
    )
    assert result is True
    close.assert_awaited_once()
    assert close.await_args.kwargs["waiting_on"] == "us"
    assert close.await_args.kwargs["incoming_message_id"] == "wamid-2"
