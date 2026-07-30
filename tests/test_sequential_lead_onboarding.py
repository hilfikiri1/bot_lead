from __future__ import annotations

import pytest

from app.services import sequential_lead_onboarding_service as service
from app.services.google_sheets_service import SpreadsheetRow


def _row(*, lead_number: str | None = None) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=167,
        phone="+48728387128",
        email="jan_ovo@wp.pl",
        client_name="Andrzej Janka",
        company=None,
        product="narzędzia",
        lead_number=lead_number,
        lead_status=None,
        marketing_comment=None,
        budget="$5_000_-_$10_000",
        contact_channel="whats_app",
        region="kujawsko pomorskie",
    )


def _item() -> dict:
    return {
        "kommo_lead_id": 15402709,
        "kommo_old_name": "Facebook #12312412421",
        "kommo_url": "https://example.kommo.com/leads/detail/15402709",
        "current_status_id": 1,
        "target_status_id": 2,
        "row_number": 167,
        "lead_number": "167",
        "row_fingerprint": [
            "+48728387128",
            "jan_ovo@wp.pl",
            "andrzej janka",
            "narzędzia",
        ],
        "old_comment": "",
        "client_name": "Andrzej Janka",
        "phone": "+48728387128",
        "email": "jan_ovo@wp.pl",
        "product_original": "narzędzia",
        "product_ru": "Инструменты",
        "proposed_name": "167 - Инструменты",
        "budget": "$5_000_-_$10_000",
        "channel": "whats_app",
        "region": "kujawsko pomorskie",
        "matched_by": "phone",
        "task_due_at": 2_000_000_000,
        "analysis": {
            "personal_analysis": "Категория слишком широкая; требуется квалификация.",
            "potential": "средний",
            "readiness": "низкая",
            "priority": "C",
            "priority_reason": "Требуется квалификация.",
            "risks": ["неизвестен перечень"],
            "missing_data": ["перечень", "количество", "город доставки"],
            "contact_method": "whatsapp",
            "contact_reason": "Сначала получить конкретику письменно.",
            "recommended_action": "Написать в WhatsApp.",
            "client_message": "Dzień dobry Panie Andrzeju, proszę o listę produktów.",
            "call_script": "",
            "task_text": "Получить перечень инструментов и количество",
            "followup_plan": "Проверить ответ завтра.",
        },
    }


def test_facebook_title_and_custom_form_identity_are_recognized():
    assert service.is_facebook_lead_name("Facebook #12312412421")
    assert service.is_facebook_lead_name("Facebook №1479023253985582")
    assert not service.is_facebook_lead_name("167 - Инструменты")

    identity = service.extract_lead_identity(
        {
            "contacts": [{"name": "Andrzej Janka", "phones": [], "emails": []}],
            "custom_fields": [
                {"name": "Proszę podać swój numer", "code": "", "value": "728387128"},
                {"name": "Poczta", "code": "", "value": "jan_ovo@wp.pl"},
                {"name": "Jakiego produktu", "code": "", "value": "narzędzia"},
                {"name": "Jaka wartość zamówienia", "code": "", "value": "$5_000_-_$10_000"},
                {"name": "W jaki sposób", "code": "", "value": "whats_app"},
            ],
        }
    )
    assert identity["phones"] == ["728387128"]
    assert identity["emails"] == ["jan_ovo@wp.pl"]
    assert identity["product"] == "narzędzia"
    assert identity["budget"] == "$5_000_-_$10_000"
    assert identity["channel"] == "whats_app"


def test_generic_tools_lead_prefers_written_qualification():
    analysis = service._fallback_analysis(_item())
    assert analysis["priority"] == "C"
    assert analysis["contact_method"] == "whatsapp"
    assert "Звон" not in analysis["recommended_action"]
    assert "Dzień dobry" in analysis["client_message"]
    assert "перечень" in " ".join(analysis["missing_data"])


def test_manager_card_contains_complete_decision_structure():
    text = service.format_item_card(_item(), 0, 3)
    assert "ЛИД 1 ИЗ 3" in text
    assert "167 - Инструменты" in text
    assert "Личный анализ" in text
    assert "Что нужно уточнить" in text
    assert "Готовое сообщение клиенту" in text
    assert "1. Запишет Y = 167" in text
    assert "2. Переведёт сделку на «Первый контакт»" in text


@pytest.mark.asyncio
async def test_apply_order_is_sheet_stage_note_task_final_name(monkeypatch):
    events: list[str] = []
    row = _row()
    lead_state = {"name": "Facebook #12312412421", "status_id": 1}

    monkeypatch.setattr(service.google_sheets_service, "get_rows", lambda **kwargs: [row])

    def apply_updates(updates):
        events.append("sheet")
        assert updates[0]["new_lead_number"] == "167"
        return {"updated_count": 1, "updated": updates, "skipped": []}

    monkeypatch.setattr(
        service.google_sheets_service, "apply_lead_registry_updates", apply_updates
    )

    async def get_details(lead_id):
        return {
            "id": lead_id,
            "name": lead_state["name"],
            "status_id": lead_state["status_id"],
            "contacts": [],
            "custom_fields": [],
            "url": "https://example.kommo.com/leads/detail/15402709",
        }

    async def update_lead(lead_id, **kwargs):
        if "status_id" in kwargs:
            events.append("stage")
            lead_state["status_id"] = kwargs["status_id"]
        if "name" in kwargs:
            events.append("name")
            lead_state["name"] = kwargs["name"]
        return {"id": lead_id, **kwargs}

    async def notes(lead_id, limit=50):
        return []

    async def add_note(lead_id, text):
        events.append("note")
        assert "ЛИЧНЫЙ АНАЛИЗ" in text
        return True

    async def tasks(lead_id, limit=50):
        return []

    async def create_task(**kwargs):
        events.append("task")
        assert "№167" in kwargs["text"]
        return {"id": 1}

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(service.kommo_service, "get_lead_details", get_details)
    monkeypatch.setattr(service.kommo_service, "update_kommo_lead", update_lead)
    monkeypatch.setattr(service.kommo_service, "get_recent_common_notes", notes)
    monkeypatch.setattr(service.kommo_service, "add_common_note", add_note)
    monkeypatch.setattr(service.kommo_service, "get_open_lead_tasks", tasks)
    monkeypatch.setattr(service.kommo_service, "create_lead_task", create_task)
    monkeypatch.setattr(service.asyncio, "sleep", no_sleep)

    result = await service.apply_item(_item())

    assert result["success"] is True
    assert events == ["sheet", "stage", "note", "task", "name"]
    assert lead_state["name"] == "167 - Инструменты"


@pytest.mark.asyncio
async def test_queue_starts_from_unsorted_facebook_lead_and_exact_phone(monkeypatch):
    row = _row()
    monkeypatch.setattr(service.google_sheets_service, "get_rows", lambda **kwargs: [row])

    async def unsorted(**kwargs):
        return {
            "pipeline_name": "Польша (1 этап)",
            "leads": [
                {
                    "id": 15402709,
                    "name": "Facebook #12312412421",
                    "unsorted_uid": "uid-1",
                    "url": "https://example.kommo.com/leads/detail/15402709",
                }
            ],
        }

    async def details(_lead_id):
        return {
            "id": 15402709,
            "name": "Facebook #12312412421",
            "pipeline_id": 10,
            "status_id": 1,
            "contacts": [{"name": "Andrzej Janka", "phones": [], "emails": []}],
            "custom_fields": [
                {"name": "Proszę podać swój numer", "code": "", "value": "728387128"},
                {"name": "Jakiego produktu", "code": "", "value": "narzędzia"},
            ],
            "url": "https://example.kommo.com/leads/detail/15402709",
        }

    async def first_contact(_pipeline_id):
        return 2

    async def product_ru(_value):
        return "Инструменты"

    async def analysis(item):
        return service._fallback_analysis(item)

    monkeypatch.setattr(service.kommo_service, "get_all_unsorted_leads", unsorted)
    monkeypatch.setattr(service.kommo_service, "get_lead_details", details)
    monkeypatch.setattr(service.lead_status_sync_service, "_first_contact_status_id", first_contact)
    monkeypatch.setattr(service, "_short_product_ru", product_ru)
    monkeypatch.setattr(service, "_generate_analysis", analysis)

    report = await service.build_onboarding_queue()

    assert report["matched_count"] == 1
    assert report["items"][0]["lead_number"] == "167"
    assert report["items"][0]["proposed_name"] == "167 - Инструменты"
    assert report["items"][0]["matched_by"] == "phone"
