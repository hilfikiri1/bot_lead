from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest

from app.agent import memory
from app.agent import service as agent_service
from app.agent.contracts import AgentReply
from app.services import lead_status_sync_service, telegram_state_service
from app.services import critical_workflow_runtime as runtime_module


def _report(task_due_at: int) -> dict:
    return {
        "sheet_updates": [
            {
                "row_number": 166,
                "new_lead_number": "166",
                "kommo_lead_id": 77,
            }
        ],
        "onboarding_actions": [
            {
                "row_number": 166,
                "kommo_lead_id": 77,
                "lead_number": "166",
                "old_name": "Facebook lead",
                "new_name": "166 - Чай",
                "target_status_id": None,
                "task_text": "Позвонить клиенту",
                "task_due_at": task_due_at,
                "number_conflict": None,
            }
        ],
    }


def test_registry_digest_ignores_recalculated_task_due_timestamp():
    first = runtime_module.stable_registry_digest(_report(1_000))
    second = runtime_module.stable_registry_digest(_report(9_999))
    assert first == second


def test_registry_digest_changes_when_real_preview_changes():
    first = _report(1_000)
    second = _report(1_000)
    second["onboarding_actions"][0]["new_name"] = "166 - Другой товар"
    assert runtime_module.stable_registry_digest(first) != runtime_module.stable_registry_digest(second)


def test_project_upload_state_requires_explicit_mode_and_lead():
    assert runtime_module._project_upload_lead_id(None) is None
    assert runtime_module._project_upload_lead_id({"mode": "other", "kommo_lead_id": 77}) is None
    assert runtime_module._project_upload_lead_id({"mode": "awaiting_project_file"}) is None
    assert runtime_module._project_upload_lead_id(
        {"mode": "awaiting_project_file", "kommo_lead_id": "77"}
    ) == 77


@pytest.mark.asyncio
async def test_runtime_binds_file_to_selected_project(monkeypatch):
    runtime = importlib.reload(runtime_module)

    original_callback = AsyncMock(
        return_value=AgentReply(
            "Отправь файл",
            intent="project_upload_prompt",
            metadata={"lead_id": 77},
        )
    )
    original_upload = AsyncMock(
        return_value=AgentReply("Подтвердить загрузку", intent="save_file_to_drive_project")
    )
    monkeypatch.setattr(agent_service, "handle_callback", original_callback)
    monkeypatch.setattr(agent_service, "handle_project_file_upload", original_upload)
    monkeypatch.setattr(lead_status_sync_service, "_updates_digest", lambda report: "old")

    set_state = AsyncMock()
    get_state = AsyncMock(
        return_value={
            "mode": "awaiting_project_file",
            "kommo_lead_id": 77,
            "chat_id": 10,
        }
    )
    clear_state = AsyncMock()
    monkeypatch.setattr(telegram_state_service, "set_state", set_state)
    monkeypatch.setattr(telegram_state_service, "get_state", get_state)
    monkeypatch.setattr(telegram_state_service, "clear_state", clear_state)

    session = object()
    get_session = AsyncMock(return_value=session)
    set_active = AsyncMock()
    monkeypatch.setattr(memory, "get_or_create_session", get_session)
    monkeypatch.setattr(memory, "set_active_lead", set_active)

    runtime.install_critical_workflow_runtime()

    prompt = await agent_service.handle_callback(
        None,
        callback_data="agent:project:upload:77",
        telegram_user_id=5,
        chat_id=10,
    )
    assert "Выбранный проект зафиксирован" in prompt.text
    set_state.assert_awaited_once()
    assert set_state.await_args.args[0] == 5
    assert set_state.await_args.args[1]["kommo_lead_id"] == 77

    result = await agent_service.handle_project_file_upload(
        None,
        chat_id=10,
        telegram_user_id=5,
        telegram_message_id=100,
        filename="offer.pdf",
        mime_type="application/pdf",
        content=b"pdf",
        caption=None,
        kind="document",
    )

    assert result.intent == "save_file_to_drive_project"
    get_session.assert_awaited_once_with(None, telegram_user_id=5)
    set_active.assert_awaited_once_with(
        None,
        session=session,
        kommo_lead_id=77,
    )
    original_upload.assert_awaited_once()
    clear_state.assert_awaited_once_with(5)
    assert lead_status_sync_service._updates_digest(_report(1_000)) == runtime.stable_registry_digest(
        _report(1_000)
    )
