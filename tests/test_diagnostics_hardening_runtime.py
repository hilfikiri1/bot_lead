from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services import diagnostics_hardening_runtime as runtime


def test_safe_value_keeps_diagnostic_details_and_secret_states() -> None:
    value = {
        "configuration": {
            "variables": {"WHATSAPP_ACCESS_TOKEN": "MISSING"},
        },
        "checks": [
            {
                "data": {
                    "events": [
                        {
                            "service": "client_message",
                            "operation": "send",
                            "status": "error",
                            "error_message": "Drive permission denied",
                        }
                    ]
                }
            }
        ],
        "safety": {"secrets_included": False},
    }

    safe = runtime._safe_value(value)
    assert safe["configuration"]["variables"]["WHATSAPP_ACCESS_TOKEN"] == "MISSING"
    assert safe["checks"][0]["data"]["events"][0]["service"] == "client_message"
    assert safe["checks"][0]["data"]["events"][0]["error_message"] == "Drive permission denied"
    assert safe["safety"]["secrets_included"] is False

    rendered = json.loads(runtime._render_diagnostic_json(value))
    assert rendered["checks"][0]["data"]["events"][0]["operation"] == "send"


def test_pipeline_country_uses_poland_pipeline() -> None:
    assert runtime._pipeline_country({"pipeline_name": "Польша (1 этап)"}) == "PL"
    assert runtime._pipeline_country({"pipeline_name": "Germany B2B"}) == "DE"
    assert runtime._pipeline_country({"pipeline_name": "General"}) is None


@pytest.mark.asyncio
async def test_whatsapp_disabled_is_skip(monkeypatch) -> None:
    monkeypatch.setattr(runtime.settings, "whatsapp_enabled", False)
    monkeypatch.setattr(runtime.settings, "whatsapp_phone_number_id", "1303777606144608")
    monkeypatch.setattr(runtime.settings, "whatsapp_access_token", "")
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)

    result = await runtime._whatsapp_check()

    assert result["status"] == "SKIP"
    assert result["data"]["phone_number_id"] == "SET"
    assert result["data"]["access_token"] == "EMPTY"


@pytest.mark.asyncio
async def test_drive_check_fails_when_projects_folder_is_read_only(monkeypatch) -> None:
    async def fake_original():
        return {
            "name": "google_drive",
            "status": "PASS",
            "detail": "read ok",
            "duration_ms": 0,
            "data": {},
            "recommendation": None,
        }

    async def fake_verify(folder_id: str):
        return {
            "name": folder_id,
            "driveId": "drive-1",
            "can_add_children": folder_id != "projects",
            "can_list_children": True,
            "can_edit": True,
        }

    monkeypatch.setattr(runtime, "_ORIGINAL_DRIVE_CHECK", fake_original)
    monkeypatch.setattr(runtime, "_verify_folder_access", fake_verify)
    monkeypatch.setattr(runtime.settings, "google_drive_root_folder_id", "root")
    monkeypatch.setattr(runtime.settings, "google_drive_projects_folder_id", "projects")

    result = await runtime._drive_check()

    assert result["status"] == "FAIL"
    assert result["data"]["folder_capabilities"]["projects"]["can_add_children"] is False


@pytest.mark.asyncio
async def test_legacy_folder_requires_unique_strong_match(monkeypatch) -> None:
    async def fake_folders(parent_id: str):
        assert parent_id == "root"
        return [
            {"id": "a", "name": "BBS-OTHER-0107 — 107 Кисточки для макияжа"},
            {"id": "b", "name": "BBS-PL-0108 — Другой проект"},
        ]

    monkeypatch.setattr(runtime, "_list_child_folders", fake_folders)

    found = await runtime._find_legacy_project_folder(
        parent_id="root",
        internal_number="107",
        lead_name="107 Кисточки для макияжа",
        kommo_lead_id=10242103,
    )

    assert found is not None
    assert found["id"] == "a"


@pytest.mark.asyncio
async def test_project_link_is_persisted_before_subfolder_creation(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_country_folder(*, root_id: str, country_code: str):
        return {"id": "pl", "name": "Польша"}

    async def fake_get_link(db, lead_id):
        return None

    async def fake_find(*, parent_id: str, project_key: str):
        if parent_id == "pl":
            return {
                "id": "folder-107",
                "name": "BBS-PL-0107 — 107 Кисточки для макияжа",
                "parents": ["pl"],
                "webViewLink": "https://drive.example/folder-107",
            }
        return None

    async def fake_move(*, folder, target_parent_id):
        return folder, False, None

    async def fake_upsert(db, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            project_key=kwargs["project_key"],
            drive_folder_id=kwargs["drive_folder_id"],
            drive_folder_url=kwargs["drive_folder_url"],
        )

    async def fail_subfolders(folder_id: str):
        raise RuntimeError("subfolder failed")

    monkeypatch.setattr(runtime.settings, "google_drive_projects_folder_id", "root")
    monkeypatch.setattr(runtime, "_ensure_country_folder", fake_country_folder)
    monkeypatch.setattr(runtime.project_link_service, "get_by_kommo_lead_id", fake_get_link)
    monkeypatch.setattr(runtime, "_find_project_folder", fake_find)
    monkeypatch.setattr(runtime, "_move_folder", fake_move)
    monkeypatch.setattr(runtime.project_link_service, "upsert_link", fake_upsert)
    monkeypatch.setattr(runtime.google_drive_service, "ensure_project_subfolders", fail_subfolders)

    with pytest.raises(RuntimeError, match="subfolder failed"):
        await runtime._execute_drive_project(
            object(),
            payload={
                "kommo_lead_id": 10242103,
                "project_key": "BBS-PL-0107",
                "internal_lead_number": "107",
                "kommo_lead_name": "107 Кисточки для макияжа",
                "project_name": "107 Кисточки для макияжа",
                "client_name": "Monika Szewczyk",
                "country_code": "PL",
            },
        )

    assert len(calls) == 1
    assert calls[0]["drive_folder_id"] == "folder-107"
    assert calls[0]["metadata"]["folder_link_phase"] == "linked"
