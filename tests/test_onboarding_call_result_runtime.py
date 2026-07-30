from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.agent import service as agent_service
from app.agent.contracts import AgentReply
from app.models.agent_session import AgentSession
from app.services import onboarding_call_result_runtime as runtime
from app.services.onboarding_call_checkin_runtime import _checkin_markup


def _session() -> AgentSession:
    return AgentSession(
        telegram_user_id=99,
        active_kommo_lead_id=15402709,
        context={
            "active_internal_lead_number": "167",
            "pending_call_result": {
                "lead_id": 15402709,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            "onboarding_call_checkins": [
                {
                    "lead_id": 15402709,
                    "lead_number": "167",
                    "status": "awaiting_result",
                }
            ],
        },
    )


def test_call_checkin_callback_stays_within_telegram_limit():
    markup = _checkin_markup(
        {
            "lead_id": 15402709,
            "kommo_url": "https://example.kommo.com/leads/detail/15402709",
        }
    )
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]
    assert callbacks == ["onboard:call:15402709"]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_pending_call_result_expiry():
    fresh = {"started_at": datetime.now(timezone.utc).isoformat()}
    old = {"started_at": (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()}
    assert runtime._pending_is_fresh(fresh) is True
    assert runtime._pending_is_fresh(old) is False
    assert runtime._pending_is_fresh({"started_at": "broken"}) is False


@pytest.mark.asyncio
async def test_next_voice_is_forced_into_selected_project(monkeypatch):
    session = _session()
    db = AsyncMock()
    original = AsyncMock(return_value=AgentReply("preview", intent="project_update_bundle"))

    monkeypatch.setattr(agent_service, "handle_message", original)
    monkeypatch.setattr(
        agent_service.memory,
        "get_or_create_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(runtime, "_INSTALLED", False)
    runtime.install_onboarding_call_result_runtime()

    reply = await agent_service.handle_message(
        db,
        chat_id=1,
        telegram_user_id=99,
        text="Клиент подтвердил 100 штук, список пришлёт завтра",
        source="voice",
        allow_conversation_passthrough=True,
    )

    assert reply.intent == "project_update_bundle"
    kwargs = original.await_args.kwargs
    assert kwargs["active_kommo_lead_id"] == 15402709
    assert kwargs["allow_conversation_passthrough"] is False
    assert kwargs["source"] == "voice"
    assert kwargs["text"].startswith(
        "По проекту 167 поговорил с клиентом. Результат разговора:"
    )
    assert session.context["pending_call_result"] is None
    assert session.context["onboarding_call_checkins"][0]["status"] == "captured"
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_slash_command_bypasses_pending_call_result(monkeypatch):
    session = _session()
    db = AsyncMock()
    original = AsyncMock(return_value=AgentReply("diag", intent="diagnostics"))

    monkeypatch.setattr(agent_service, "handle_message", original)
    monkeypatch.setattr(
        agent_service.memory,
        "get_or_create_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(runtime, "_INSTALLED", False)
    runtime.install_onboarding_call_result_runtime()

    reply = await agent_service.handle_message(
        db,
        chat_id=1,
        telegram_user_id=99,
        text="/diag 167",
        source="text",
    )

    assert reply.intent == "diagnostics"
    assert original.await_args.kwargs["text"] == "/diag 167"
    assert session.context["pending_call_result"]["lead_id"] == 15402709


@pytest.mark.asyncio
async def test_expired_pending_state_falls_back_without_retargeting(monkeypatch):
    session = _session()
    session.context["pending_call_result"]["started_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=13)
    ).isoformat()
    db = AsyncMock()
    original = AsyncMock(return_value=AgentReply("normal", intent="conversation"))

    monkeypatch.setattr(agent_service, "handle_message", original)
    monkeypatch.setattr(
        agent_service.memory,
        "get_or_create_session",
        AsyncMock(return_value=session),
    )
    monkeypatch.setattr(runtime, "_INSTALLED", False)
    runtime.install_onboarding_call_result_runtime()

    await agent_service.handle_message(
        db,
        chat_id=1,
        telegram_user_id=99,
        text="Обычное сообщение",
        source="text",
    )

    assert original.await_args.kwargs["text"] == "Обычное сообщение"
    assert original.await_args.kwargs["active_kommo_lead_id"] is None
    assert session.context["pending_call_result"] is None
    assert session.context["onboarding_call_checkins"][0]["status"] == "expired"
