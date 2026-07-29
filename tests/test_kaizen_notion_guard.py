from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.contracts import AgentReply
from app.services import kaizen_notion_guard_runtime


@pytest.mark.asyncio
async def test_capability_rejects_missing_required_properties(monkeypatch):
    monkeypatch.setattr(kaizen_notion_guard_runtime.settings, "notion_api_token", "token")
    monkeypatch.setattr(
        kaizen_notion_guard_runtime.settings,
        "notion_tasks_data_source_id",
        "tasks",
    )
    monkeypatch.setattr(
        kaizen_notion_guard_runtime.notion_gateway,
        "retrieve_data_source",
        AsyncMock(return_value={"properties": {"Name": {"type": "title"}}}),
    )
    ready, reason = await kaizen_notion_guard_runtime.notion_improvement_capability()
    assert ready is False
    assert "Тип" in reason


@pytest.mark.asyncio
async def test_capability_accepts_required_schema(monkeypatch):
    monkeypatch.setattr(kaizen_notion_guard_runtime.settings, "notion_api_token", "token")
    monkeypatch.setattr(
        kaizen_notion_guard_runtime.settings,
        "notion_tasks_data_source_id",
        "tasks",
    )
    properties = {
        "Name": {"type": "title"},
        "Type": {
            "type": "select",
            "select": {"options": [{"name": "Improvement"}]},
        },
        "Status": {
            "type": "status",
            "status": {"options": [{"name": "Todo"}]},
        },
        "Source": {
            "type": "select",
            "select": {"options": [{"name": "Kaizen"}]},
        },
    }
    monkeypatch.setattr(
        kaizen_notion_guard_runtime.notion_gateway,
        "retrieve_data_source",
        AsyncMock(return_value={"properties": properties}),
    )
    ready, reason = await kaizen_notion_guard_runtime.notion_improvement_capability()
    assert ready is True
    assert reason is None


@pytest.mark.asyncio
async def test_weekly_reply_loses_create_button_when_notion_is_down(monkeypatch):
    monkeypatch.setattr(
        kaizen_notion_guard_runtime,
        "notion_improvement_capability",
        AsyncMock(return_value=(False, "Tasks недоступен")),
    )
    reply = AgentReply(
        "📊 Итоги недели",
        intent="weekly_review",
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "Создать",
                        "callback_data": "agent:kaizen:weekcreate:12",
                    },
                    {
                        "text": "Не создавать",
                        "callback_data": "agent:kaizen:weekcancel:12",
                    },
                ],
                [
                    {
                        "text": "Пересобрать",
                        "callback_data": "agent:kaizen:weekrebuild:12",
                    }
                ],
            ]
        },
    )
    guarded = await kaizen_notion_guard_runtime.guard_weekly_reply(reply)
    callbacks = [
        button["callback_data"]
        for row in guarded.reply_markup["inline_keyboard"]
        for button in row
    ]
    assert "agent:kaizen:weekcreate:12" not in callbacks
    assert "agent:kaizen:weekcancel:12" not in callbacks
    assert "agent:kaizen:weekrebuild:12" in callbacks
    assert "Создание карточек скрыто" in guarded.text
