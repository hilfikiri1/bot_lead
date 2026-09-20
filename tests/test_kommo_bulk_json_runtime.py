from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import kommo_bulk_json_runtime as bulk


def test_parse_bulk_json_accepts_internal_and_kommo_refs():
    payload = bulk.parse_bulk_json_text(
        "/bulk_json\n"
        + json.dumps(
            {
                "version": 1,
                "actions": [
                    {
                        "lead_ref": {"internal_number": 218},
                        "action": "keep",
                    },
                    {
                        "lead_ref": {"kommo_id": 123456},
                        "action": "update",
                        "changes": {
                            "price": "18 600",
                            "stage_name": "Ожидание решения",
                        },
                        "note": "КП отправлено",
                    },
                ],
            },
            ensure_ascii=False,
        )
    )

    assert payload["version"] == 1
    assert payload["actions"][0]["lead_ref"] == {"internal_number": "218"}
    assert payload["actions"][1]["lead_ref"] == {"kommo_id": 123456}
    assert payload["actions"][1]["changes"]["price"] == 18600


def test_parse_bulk_json_rejects_duplicate_ref():
    text = json.dumps(
        {
            "version": 1,
            "actions": [
                {"lead_ref": {"internal_number": 218}, "action": "keep"},
                {
                    "lead_ref": {"internal_number": "218"},
                    "action": "add_note",
                    "note": "duplicate",
                },
            ],
        }
    )

    with pytest.raises(ValueError, match="больше одного раза"):
        bulk.parse_bulk_json_text("/bulk_json\n" + text)


@pytest.mark.asyncio
async def test_build_preview_resolves_stage_and_blocks_delete(monkeypatch):
    monkeypatch.setattr(
        bulk.kommo_service,
        "get_all_open_leads",
        AsyncMock(
            return_value={
                "leads": [
                    {
                        "id": 10001,
                        "name": "218 - laser",
                        "pipeline_id": 7,
                        "status_id": 70,
                        "status_name": "Первый контакт",
                        "url": "https://example.test/10001",
                    },
                    {
                        "id": 10002,
                        "name": "220 - trash",
                        "pipeline_id": 7,
                        "status_id": 70,
                        "status_name": "Первый контакт",
                        "url": "https://example.test/10002",
                    },
                ]
            }
        ),
    )
    monkeypatch.setattr(
        bulk.kommo_service,
        "get_pipeline_statuses",
        AsyncMock(
            return_value=[
                {"id": 70, "name": "Первый контакт"},
                {"id": 72, "name": "Ожидание решения"},
                {"id": 99, "name": "Игнор"},
            ]
        ),
    )

    report = await bulk.build_bulk_preview(
        {
            "version": 1,
            "actions": [
                {
                    "lead_ref": {"internal_number": "218"},
                    "action": "move",
                    "changes": {"stage_name": "Ожидание решения"},
                    "note": "",
                },
                {
                    "lead_ref": {"internal_number": "220"},
                    "action": "delete",
                    "changes": {},
                    "note": "",
                },
            ],
        }
    )

    assert report["unresolved"] == []
    first, second = report["items"]
    assert first["executable"] is True
    assert first["update_payload"]["status_id"] == 72
    assert first["target_status_name"] == "Ожидание решения"
    assert second["executable"] is True
    assert second["update_payload"]["status_id"] == 99
    assert second["target_status_name"] == "Игнор"


@pytest.mark.asyncio
async def test_execute_bulk_uses_batch_helpers_and_tracks_results(monkeypatch):
    update_mock = AsyncMock(return_value=[{"id": 10001}])
    note_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        bulk.kommo_service, "update_kommo_leads_bulk", update_mock
    )
    monkeypatch.setattr(
        bulk.kommo_service, "add_common_notes_bulk", note_mock
    )

    action = SimpleNamespace(
        result=None,
        payload={
            "items": [
                {
                    "lead_id": 10001,
                    "internal_lead_number": "218",
                    "lead_name": "218 - laser",
                    "action": "update",
                    "executable": True,
                    "update_payload": {"id": 10001, "price": 18600},
                    "note_text": "КП отправлено",
                },
                {
                    "lead_id": 10002,
                    "internal_lead_number": "219",
                    "lead_name": "219 - keep",
                    "action": "keep",
                    "executable": False,
                    "update_payload": None,
                    "note_text": "",
                },
            ],
            "item_results": {},
        },
    )

    result = await bulk._execute_bulk_json(action)

    update_mock.assert_awaited_once()
    note_mock.assert_awaited_once()
    assert result["partial_failed"] is False
    assert action.payload["item_results"]["10001"]["status"] == "ok"
    assert action.payload["item_results"]["10002"]["status"] == "ok"
