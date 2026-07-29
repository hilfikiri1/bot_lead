from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import kaizen_runtime


def _candidate(title: str) -> dict:
    return {
        "title": title,
        "problem": "Актуальные файлы разбросаны",
        "evidence": "Проблема упоминалась в нескольких днях",
        "proposed_action": "Хранить финальную версию в папке проекта",
        "expected_effect": "Сократить повторный поиск",
        "verification": "Через неделю проверить повторные поиски",
        "impact": "high",
        "effort": "low",
        "priority": 1,
        "due_date": None,
    }


def _entry():
    return SimpleNamespace(
        id=44,
        telegram_user_id=101,
        entry_type="weekly",
        period_start=date(2026, 7, 27),
        period_end=date(2026, 8, 2),
        status="completed",
        analysis={
            "completed_days": 4,
            "improvement_candidates": [
                _candidate("Единая папка прайсов"),
                _candidate("Следующий шаг после звонка"),
            ],
        },
        notion_page_ids=[],
    )


def _action(entry):
    items = list(entry.analysis["improvement_candidates"])
    return SimpleNamespace(
        id=9,
        telegram_user_id=101,
        payload={
            "telegram_user_id": 101,
            "weekly_entry_id": entry.id,
            "week_start": entry.period_start.isoformat(),
            "week_end": entry.period_end.isoformat(),
            "items": items,
            "item_results": {},
        },
    )


@pytest.fixture(autouse=True)
def _simple_json_flag(monkeypatch):
    monkeypatch.setattr(kaizen_runtime, "flag_modified", lambda *_args, **_kwargs: None)


@pytest.mark.asyncio
async def test_partial_notion_batch_is_saved_and_retry_does_not_duplicate(monkeypatch):
    entry = _entry()
    action = _action(entry)
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(
        kaizen_runtime.kaizen_journal_service,
        "get_entry_by_id",
        AsyncMock(return_value=entry),
    )
    create = AsyncMock(
        side_effect=[
            {
                "id": "page-1",
                "url": "https://notion.so/page-1",
                "created": True,
            },
            RuntimeError("Notion temporary error"),
        ]
    )
    monkeypatch.setattr(
        kaizen_runtime.kaizen_journal_service,
        "create_notion_improvement_page",
        create,
    )

    first = await kaizen_runtime._execute_notion_improvements(db, action)

    assert first["partial_failed"] is True
    assert len(entry.notion_page_ids) == 1
    assert entry.notion_page_ids[0]["page_id"] == "page-1"
    assert action.payload["item_results"]["kaizen:44:1"]["status"] == "ok"
    assert action.payload["item_results"]["kaizen:44:2"]["status"] == "failed"
    assert create.await_count == 2

    create.reset_mock()
    create.side_effect = None
    create.return_value = {
        "id": "page-2",
        "url": "https://notion.so/page-2",
        "created": True,
    }

    second = await kaizen_runtime._execute_notion_improvements(db, action)

    assert second["partial_failed"] is False
    assert create.await_count == 1
    assert len(entry.notion_page_ids) == 2
    assert {item["page_id"] for item in entry.notion_page_ids} == {"page-1", "page-2"}
    assert action.payload["item_results"]["kaizen:44:1"]["status"] == "ok"
    assert action.payload["item_results"]["kaizen:44:2"]["status"] == "ok"


@pytest.mark.asyncio
async def test_notion_batch_rejects_another_user(monkeypatch):
    entry = _entry()
    action = _action(entry)
    action.telegram_user_id = 202
    db = SimpleNamespace(commit=AsyncMock())

    with pytest.raises(PermissionError):
        await kaizen_runtime._execute_notion_improvements(db, action)


@pytest.mark.asyncio
async def test_notion_batch_revalidates_week_period(monkeypatch):
    entry = _entry()
    action = _action(entry)
    action.payload["week_end"] = "2026-08-09"
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(
        kaizen_runtime.kaizen_journal_service,
        "get_entry_by_id",
        AsyncMock(return_value=entry),
    )

    with pytest.raises(ValueError, match="Период"):
        await kaizen_runtime._execute_notion_improvements(db, action)
