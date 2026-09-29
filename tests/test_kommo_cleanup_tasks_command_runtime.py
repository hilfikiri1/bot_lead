from __future__ import annotations

import pytest

from app.services import kommo_cleanup_tasks_command_runtime as cleanup


def test_cleanup_command_matches_plain_and_bot_suffix():
    assert cleanup._is_cleanup_command("/cleanup_tasks") is True
    assert cleanup._is_cleanup_command("/cleanup_tasks@BBS_Poland_Bot") is True
    assert cleanup._is_cleanup_command(" /cleanup_tasks ") is True
    assert cleanup._is_cleanup_command("/cleanup_task") is False


@pytest.mark.asyncio
async def test_build_cleanup_payload_uses_unique_open_lead_ids(monkeypatch):
    async def fake_get_all_open_leads():
        return {
            "leads": [
                {"id": 101, "name": "A"},
                {"id": 102, "name": "B"},
                {"id": 101, "name": "A duplicate"},
                {"id": None, "name": "broken"},
            ]
        }

    monkeypatch.setattr(cleanup.kommo_service, "get_all_open_leads", fake_get_all_open_leads)

    payload = await cleanup._build_cleanup_payload()

    assert payload == {
        "version": 1,
        "actions": [
            {"lead_ref": {"kommo_id": 101}, "action": "delete_overdue_tasks"},
            {"lead_ref": {"kommo_id": 102}, "action": "delete_overdue_tasks"},
        ],
    }
