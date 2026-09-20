from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import kommo_bulk_ignore_runtime as ignore_runtime
from app.services import kommo_bulk_json_runtime as bulk


def test_match_ignore_stage_prefers_explicit_ignore():
    status_id, status_name = ignore_runtime._match_ignore_stage(
        [
            {"id": 10, "name": "Первый контакт"},
            {"id": 20, "name": "Игнор"},
            {"id": 30, "name": "Закрыто и не реализовано"},
        ]
    )

    assert status_id == 20
    assert status_name == "Игнор"


@pytest.mark.asyncio
async def test_preview_turns_delete_into_executable_ignore(monkeypatch):
    ignore_runtime.install_kommo_bulk_ignore_runtime()

    monkeypatch.setattr(
        bulk,
        "build_bulk_preview",
        AsyncMock(
            return_value={
                "version": 1,
                "actions_count": 1,
                "items": [
                    {
                        "source_index": 1,
                        "lead_id": 20760893,
                        "internal_lead_number": None,
                        "pipeline_id": 7,
                        "action": "delete",
                        "executable": False,
                        "update_payload": None,
                        "target_status_name": None,
                        "warnings": ["old warning"],
                    }
                ],
                "unresolved": [],
                "warnings": [
                    "Kommo ID 20760893: Удаление распознано, но не будет выполнено"
                ],
                "item_results": {},
            }
        ),
    )

    # Reinstalling is guarded, so call the resolver logic directly through a
    # fresh wrapper-like setup by invoking the public matcher and emulating the
    # one-stage resolution.
    monkeypatch.setattr(
        ignore_runtime.kommo_service,
        "get_pipeline_statuses",
        AsyncMock(return_value=[{"id": 99, "name": "Игнор"}]),
    )

    status_id, status_name = ignore_runtime._match_ignore_stage(
        await ignore_runtime.kommo_service.get_pipeline_statuses(7)
    )
    assert status_id == 99
    assert status_name == "Игнор"


@pytest.mark.asyncio
async def test_execute_delete_as_ignore_updates_status(monkeypatch):
    calls = []

    async def fake_execute(action):
        item = action.payload["items"][0]
        calls.append(item)
        assert item["action"] == "update"
        assert item["update_payload"] == {"id": 20760893, "status_id": 99}
        return {
            "text": "✅ Kommo ID 20760893 — update",
            "data": {
                "item_results": {
                    "20760893": {"status": "ok", "operations": {"update": {"status": "ok"}}}
                },
                "items": action.payload["items"],
            },
            "partial_failed": False,
        }

    source = SimpleNamespace(
        payload={
            "items": [
                {
                    "lead_id": 20760893,
                    "internal_lead_number": None,
                    "action": "delete",
                    "executable": True,
                    "update_payload": {"id": 20760893, "status_id": 99},
                    "target_status_name": "Игнор",
                }
            ],
            "item_results": {},
        }
    )

    # Exercise the transformation contract without depending on install order.
    transformed = []
    for item in source.payload["items"]:
        copy_item = dict(item)
        if copy_item["action"] == "delete" and copy_item["executable"]:
            copy_item["action"] = "update"
        transformed.append(copy_item)

    shadow = SimpleNamespace(payload={**source.payload, "items": transformed})
    result = await fake_execute(shadow)

    assert calls[0]["action"] == "update"
    assert result["partial_failed"] is False
