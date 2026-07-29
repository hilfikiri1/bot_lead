from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agent.planner import deterministic_plan
from app.config import Settings
from app.services import (
    client_language_service,
    client_message_service,
    identity_service,
)


def _user(
    *,
    role: str = "manager",
    scope: str = "assigned",
    kommo_user_id: int | None = 77,
):
    return SimpleNamespace(
        id=1,
        telegram_user_id=100,
        telegram_username="manager",
        display_name="Manager",
        role=role,
        status="active",
        lead_access_scope=scope,
        kommo_user_id=kommo_user_id,
    )


def test_followup_without_explicit_language_uses_auto_resolution():
    plan = deterministic_plan(
        "подготовь follow-up по этой сделке",
        {"active_kommo_lead_id": 123},
    )
    assert plan is not None
    assert plan.intent == "generate_draft"
    assert plan.language == "auto"


def test_explicit_polish_still_has_highest_priority():
    plan = deterministic_plan(
        "напиши клиенту по-польски по этой сделке",
        {"active_kommo_lead_id": 123},
    )
    assert plan is not None
    assert plan.language == "pl"


def test_polish_history_is_detected_conservatively():
    result = client_language_service.infer_language_from_history(
        "Dzień dobry, dziękuję za ofertę. Proszę przesłać termin realizacji. Pozdrawiam."
    )
    assert result is not None
    assert result[0] == "pl"
    assert result[1] > 0.7


def test_ukrainian_history_is_detected():
    result = client_language_service.infer_language_from_history(
        "Добрий день, дякую за інформацію. Будь ласка, надішліть умови замовлення."
    )
    assert result is not None
    assert result[0] == "uk"


def test_phone_prefix_sets_market_fallback():
    assert client_language_service.infer_direction_language(
        {"contacts": [{"phones": ["+48 790 870 113"]}]}
    ) == ("pl", 0.93)
    assert client_language_service.infer_direction_language(
        {"contacts": [{"phones": ["+380 97 973 57 89"]}]}
    ) == ("uk", 0.93)


def test_internal_russian_note_is_not_treated_as_client_correspondence():
    lead = {
        "notes": [
            {"text": "Добрый день. Внутренняя заметка: клиент ждёт цену."},
            {"text": "WhatsApp: Dzień dobry, proszę przesłać ofertę."},
        ]
    }
    history = client_language_service.correspondence_history(lead)
    assert "Внутренняя заметка" not in history
    assert "Dzień dobry" in history


def test_whatsapp_link_normalizes_polish_local_number_and_encodes_text():
    url = client_message_service.whatsapp_click_to_chat_url(
        "790 870 113",
        "Dzień dobry! Cena: 10 EUR",
        language="pl",
    )
    assert url.startswith("https://wa.me/48790870113?text=")
    assert "Dzie%C5%84%20dobry%21" in url


def test_invalid_phone_is_rejected():
    with pytest.raises(ValueError, match="międzynarodowego|международного"):
        client_message_service.whatsapp_click_to_chat_url("123", "Test", language="pl")


def test_vcard_contains_contact_fields_and_escapes_company():
    content = client_message_service.build_vcard(
        name="Maciej Walasek",
        company="MasterTech; Polska",
        phone="+48 790 870 113",
        email="maciej@example.pl",
        language="pl",
    ).decode("utf-8")
    assert "BEGIN:VCARD" in content
    assert "FN:Maciej Walasek" in content
    assert "ORG:MasterTech\\; Polska" in content
    assert "TEL;TYPE=CELL:+48790870113" in content
    assert "EMAIL;TYPE=INTERNET:maciej@example.pl" in content


def test_whatsapp_markup_has_open_edit_language_vcard_and_sent_controls():
    record = SimpleNamespace(
        id=9,
        channel="whatsapp",
        recipient="+48790870113",
        body="Dzień dobry",
        communication_language="pl",
    )
    markup = client_message_service.message_draft_markup(record)
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    labels = {button["text"] for button in buttons}
    assert "💬 Открыть WhatsApp" in labels
    assert "✏️ Изменить текст" in labels
    assert "👤 Контакт .vcf" in labels
    assert {"PL", "UA", "RU"}.issubset(labels)
    assert "✅ Да, отметить в Kommo" in labels


def test_owner_and_admin_can_manage_users_but_viewer_cannot():
    assert identity_service.can_manage_users(_user(role="owner", scope="all"))
    assert identity_service.can_manage_users(_user(role="admin", scope="all"))
    assert not identity_service.can_manage_users(_user(role="viewer", scope="all"))
    assert identity_service.can_invite_role(_user(role="owner"), "admin")
    assert not identity_service.can_invite_role(_user(role="admin"), "admin")


def test_manager_sees_only_assigned_kommo_leads():
    manager = _user(role="manager", scope="assigned", kommo_user_id=77)
    assert identity_service.user_can_access_responsible_id(manager, 77)
    assert not identity_service.user_can_access_responsible_id(manager, 88)
    unbound = _user(role="manager", scope="assigned", kommo_user_id=None)
    assert not identity_service.user_can_access_responsible_id(unbound, 77)


def test_viewer_cannot_confirm_writes():
    assert not identity_service.can_write(_user(role="viewer", scope="all"))
    assert identity_service.can_write(_user(role="manager"))


def test_both_telegram_allowlist_variable_names_are_accepted():
    settings = Settings(
        allowed_telegram_user_ids="111,222",
        telegram_allowed_user_ids="222,333",
    )
    assert settings.get_allowed_user_ids() == [111, 222, 333]


def test_client_message_audit_fields_exist():
    from app.models.client_message_draft import ClientMessageDraft

    columns = set(ClientMessageDraft.__table__.columns.keys())
    assert {
        "prepared_by_user_id",
        "last_edited_by_user_id",
        "sent_confirmed_by_user_id",
        "sent_by_user_id",
        "delivery_marker",
        "sent_at",
    }.issubset(columns)


def test_pending_action_tracks_approver_and_executor():
    from app.models.pending_agent_action import PendingAgentAction

    columns = set(PendingAgentAction.__table__.columns.keys())
    assert "approved_by_telegram_user_id" in columns
    assert "executed_by_telegram_user_id" in columns


def test_default_client_language_is_polish():
    with patch.object(
        client_language_service.settings, "agent_default_client_language", "pl"
    ):
        assert (
            client_language_service.normalize_language(
                client_language_service.settings.agent_default_client_language
            )
            == "pl"
        )


def test_direct_audio_background_helper_is_available():
    from app.api import telegram as telegram_api

    assert callable(telegram_api._spawn_background)


def test_viewer_guard_covers_legacy_confirmation_callbacks():
    from app.api import telegram as telegram_api

    assert telegram_api._legacy_callback_requires_write("action:gmail:1:2")
    assert telegram_api._legacy_callback_requires_write("note:confirm:1:1")
    assert not telegram_api._legacy_callback_requires_write("menu:leads:1")
