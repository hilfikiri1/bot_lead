"""Hotfix regressions: Telegram routing for v5 commands + Kommo phone extraction."""

from pathlib import Path

from app.agent.planner import deterministic_plan
from app.services.kommo_service import _contact_channels
from app.services.contact_resolver import resolve_contact


def test_telegram_routes_v5_slash_commands_to_agent():
    source = Path("app/api/telegram.py").read_text(encoding="utf-8")
    for command in (
        "/plan",
        "/inbox",
        "/overdue",
        "/without_next",
        "/drive_status",
        "/integration_status",
        "/history",
        "/sheets_sync_preview",
    ):
        assert f'"{command}"' in source
    assert "Unknown slash commands" in source or "falling through to the main menu" in source


def test_planner_accepts_plain_plan_inbox_overdue_drive_status():
    for text, intent in (
        ("plan", "daily_plan"),
        ("/plan", "daily_plan"),
        ("inbox", "project_inbox"),
        ("overdue", "overdue_actions"),
        ("drive status", "drive_status"),
        ("/drive_status", "drive_status"),
    ):
        plan = deterministic_plan(text, {})
        assert plan is not None, text
        assert plan.intent == intent, text


def test_kommo_phone_extracted_from_field_name_without_code():
    contact = {
        "custom_fields_values": [
            {
                "field_name": "Phone",
                "field_code": None,
                "field_type": "multitext",
                "values": [{"value": "+48 501 407 028"}],
            }
        ]
    }
    phones, emails = _contact_channels(contact)
    assert phones == ["+48 501 407 028"]
    assert emails == []


def test_kommo_phone_still_works_with_field_code():
    contact = {
        "custom_fields_values": [
            {
                "field_name": "Work phone",
                "field_code": "PHONE",
                "values": [{"value": "+48 501 407 028"}],
            }
        ]
    }
    phones, _ = _contact_channels(contact)
    assert phones[0] == "+48 501 407 028"


def test_resolver_reads_phone_from_flattened_custom_fields():
    lead = {
        "id": 1,
        "name": "117 — test",
        "contacts": [
            {
                "id": 9,
                "name": "Anna Mosińska",
                "phones": [],
                "emails": [],
                "custom_fields": {"Phone": "+48 501 407 028"},
            }
        ],
    }
    contact = resolve_contact(lead)
    assert contact.phone_normalized == "48501407028"
    assert contact.name == "Anna Mosińska"


def test_resolver_reads_polish_lead_form_phone_when_contact_empty():
    """Lead 143-like: Messenger contact empty, phone in form custom field."""
    lead = {
        "id": 12886003,
        "name": "143 - Запчасти",
        "pipeline_name": "Польша (1 этап)",
        "status_name": "Получен ответ",
        "contacts": [
            {
                "id": 42,
                "name": "Tomasz Żagan",
                "phones": [],
                "emails": [],
                "custom_fields": [],
            }
        ],
        "custom_fields": [
            {"name": "E-mail", "code": "", "value": ""},
            {"name": "Номер телефона", "code": "PHONE", "value": ""},
            {
                "name": "Proszę podać swój numer telefonu",
                "code": "",
                "value": "512380755",
            },
            {"name": "Jaką wartość zamówienia rozważasz?", "code": "", "value": ""},
        ],
    }
    contact = resolve_contact(lead)
    assert contact.name == "Tomasz Żagan"
    assert contact.phone_normalized == "48512380755"
    assert contact.phone_display == "+48 512 380 755"
    assert contact.source == "kommo_contact_lead_phone"


def test_kommo_phone_extracted_from_polish_telefon_label():
    contact = {
        "custom_fields_values": [
            {
                "field_name": "Numer telefonu",
                "field_code": None,
                "field_type": "text",
                "values": [{"value": "512380755"}],
            }
        ]
    }
    phones, _ = _contact_channels(contact)
    assert phones == ["512380755"]
