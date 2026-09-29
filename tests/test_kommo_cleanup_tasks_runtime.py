from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import kommo_cleanup_tasks_runtime as cleanup


def test_cleanup_command_matches_exact_slash_command() -> None:
    assert cleanup._matches_command("/cleanup_tasks")
    assert cleanup._matches_command(" /cleanup_tasks@bbs_bot ")
    assert not cleanup._matches_command("/cleanup_tasks now")
    assert not cleanup._matches_command("cleanup_tasks")


@pytest.mark.asyncio
async def test_preview_collects_only_overdue_incomplete_tasks(monkeypatch) -> None:
    monkeypatch.setattr(
        cleanup.kommo_service,
        "get_all_open_leads",
        AsyncMock(
            return_value={
                "leads": [
                    {"id": 101, "name": "Laser"},
                    {"id": 202, "name": "Generator"},
                ]
            }
        ),
    )

    async def fake_tasks(lead_id: int, limit: int = 50):
        if lead_id == 101:
            return [
                {"id": 1, "is_completed": False, "complete_till": 100},
                {"id": 2, "is_completed": False, "complete_till": 2_000_000_000},
                {"id": 3, "is_completed": True, "complete_till": 100},
            ]
        return []

    monkeypatch.setattr(cleanup.kommo_service, "get_open_lead_tasks", fake_tasks)
    monkeypatch.setattr(cleanup.time, "time", lambda: 1_000)

    report = await cleanup.build_cleanup_preview()

    assert report["scanned_leads"] == 2
    assert report["leads_with_overdue"] == 1
    assert report["overdue_task_count"] == 1
    assert report["items"][0]["lead_id"] == 101
    assert report["items"][0]["overdue_task_ids"] == [1]
    assert report["scan_errors"] == []


@pytest.mark.asyncio
async def test_execute_rechecks_snapshot_and_does_not_close_new_or_moved_tasks(monkeypatch) -> None:
    action = SimpleNamespace(
        payload={
            "items": [
                {
                    "lead_id": 101,
                    "lead_name": "Laser",
                    "overdue_task_ids": [1, 2],
                    "overdue_task_count": 2,
                }
            ]
        }
    )
    monkeypatch.setattr(cleanup.time, "time", lambda: 1_000)
    monkeypatch.setattr(
        cleanup.kommo_service,
        "get_open_lead_tasks",
        AsyncMock(
            return_value=[
                # Still overdue and present in the approved snapshot: close it.
                {"id": 1, "is_completed": False, "complete_till": 100},
                # Snapshot task 2 was moved to the future: skip it.
                {"id": 2, "is_completed": False, "complete_till": 2_000},
                # New overdue task after preview: do not touch it.
                {"id": 99, "is_completed": False, "complete_till": 100},
            ]
        ),
    )
    complete = AsyncMock(return_value=1)
    monkeypatch.setattr(cleanup.overdue_runtime, "_complete_tasks", complete)

    result = await cleanup._execute_cleanup(action)

    complete.assert_awaited_once_with([1])
    assert result["data"]["completed_tasks"] == 1
    assert result["data"]["skipped_tasks"] == 1
    assert result["partial_failed"] is False
    assert action.payload["item_results"]["101"]["task_ids"] == [1]


def test_preview_without_overdue_tasks_has_no_confirmation_language() -> None:
    text = cleanup.format_cleanup_preview(
        {
            "scanned_leads": 10,
            "leads_with_overdue": 0,
            "overdue_task_count": 0,
            "items": [],
            "scan_errors": [],
        }
    )
    assert "Просроченных задач нет" in text
    assert "Никаких изменений ещё не сделано" not in text


def test_preview_blocks_execution_when_scan_is_incomplete() -> None:
    text = cleanup.format_cleanup_preview(
        {
            "scanned_leads": 10,
            "leads_with_overdue": 1,
            "overdue_task_count": 2,
            "items": [
                {
                    "lead_id": 101,
                    "lead_name": "Laser",
                    "overdue_task_count": 2,
                }
            ],
            "scan_errors": ["Kommo ID 202: timeout"],
        }
    )
    assert "выполнение заблокировано" in text
