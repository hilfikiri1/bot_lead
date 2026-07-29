"""Tests for filling empty Kommo contact fields from chat/form text."""

from app.agent.planner import deterministic_plan
from app.services import contact_hydration_service


FORM_CHAT = """
Cześć! Wypełniłem(am) formularz i chcę dowiedzieć się więcej o Twojej firmie.

W jaki sposób najlepiej się z Państwem skontaktować?:
WhatsApp

Jaką wartość zamówienia planujesz?:
$10 000 - $20 000

Jakiego produktu potrzebuje Twoja firma?:
wibratorow wgłębnych do betonu i silnikow wibracyjnych

Proszę podać swój numer kontaktowy.:
793522282

Full name:
Bartek Żaczek

W jakim regionie się Państwo znajdują?:
mazowsze

Phone number:
793 522 282

Email:
pyska84@wp.pl
"""


def test_extracts_polish_form_fields_like_engines_lead():
    extracted = contact_hydration_service.extract_contact_fields_from_text(
        FORM_CHAT, default_country="pl"
    )
    assert extracted.name == "Bartek Żaczek"
    assert "793522282" in extracted.phone.replace(" ", "")
    assert extracted.email == "pyska84@wp.pl"
    assert extracted.preferred_channel == "WhatsApp"
    assert "$10 000" in (extracted.budget_text or "")
    assert "wibrator" in (extracted.product or "")
    assert extracted.region == "mazowsze"


def test_proposal_fills_empty_contact_and_lead_fields():
    lead = {
        "id": 11307801,
        "name": "Двигатели",
        "pipeline_name": "Польша (1 этап)",
        "contacts": [
            {
                "id": 55,
                "name": "Bartek Żaczek",
                "phones": [],
                "emails": [],
                "custom_fields": [],
            }
        ],
        "custom_fields": [],
    }
    catalog = [
        {"id": 101, "name": "Номер телефона", "code": "PHONE", "type": "multitext"},
        {"id": 102, "name": "E-mail", "code": "EMAIL", "type": "multitext"},
        {"id": 103, "name": "Имя", "code": "", "type": "text"},
        {
            "id": 104,
            "name": "Proszę podać swój numer kontaktowy.",
            "code": "",
            "type": "text",
        },
        {
            "id": 105,
            "name": "Jakiego produktu potrzebuje Twoja firma?",
            "code": "",
            "type": "text",
        },
        {
            "id": 106,
            "name": "W jakim regionie się Państwo znajdują?",
            "code": "",
            "type": "text",
        },
        {
            "id": 107,
            "name": "W jaki sposób najlepiej się z Państwem skontaktować?",
            "code": "",
            "type": "text",
        },
        {
            "id": 108,
            "name": "Jaką wartość zamówienia planujesz?",
            "code": "",
            "type": "text",
        },
    ]
    proposal = contact_hydration_service.build_hydration_proposal(
        lead, corpus=FORM_CHAT, lead_field_catalog=catalog
    )
    assert proposal.has_updates
    keys = {item.key for item in proposal.updates}
    assert "phone" in keys
    assert "email" in keys
    assert "lead_phone" in keys or "lead_name_field" in keys or "product" in keys
    phone_update = next(item for item in proposal.updates if item.key == "phone")
    assert "793" in phone_update.value
    email_update = next(item for item in proposal.updates if item.key == "email")
    assert email_update.value == "pyska84@wp.pl"
    preview = contact_hydration_service.format_hydration_preview(proposal)
    assert "793" in preview
    assert "pyska84@wp.pl" in preview


def test_proposal_does_not_overwrite_existing_phone():
    lead = {
        "id": 1,
        "name": "Already filled",
        "pipeline_name": "Польша",
        "contacts": [
            {
                "id": 9,
                "name": "Bartek Żaczek",
                "phones": ["+48 793 522 282"],
                "emails": ["pyska84@wp.pl"],
            }
        ],
        "custom_fields": [],
    }
    proposal = contact_hydration_service.build_hydration_proposal(
        lead, corpus=FORM_CHAT, lead_field_catalog=[]
    )
    assert all(item.key not in {"phone", "email"} for item in proposal.updates)
    assert any("телефон уже есть" in item for item in proposal.already_filled)


def test_planner_routes_fill_contact_from_chat():
    plan = deterministic_plan(
        "заполни телефон из чата по сделке Двигатели",
        {"active_kommo_lead_id": 11307801},
    )
    assert plan is not None
    assert plan.intent == "fill_contact_from_chat"
    assert plan.mode == "write"
