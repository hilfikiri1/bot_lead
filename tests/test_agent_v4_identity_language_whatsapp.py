from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    assert "✅ Да, отметить в Kommo и Notion" in labels


@pytest.mark.asyncio
async def test_confirm_sent_writes_notion_after_kommo():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, MagicMock

    record = SimpleNamespace(
        id=11,
        kommo_lead_id=117001,
        client_name="Ewa Stokowska",
        body="Szanowna Pani Ewo...",
        communication_language="pl",
        delivery_marker="BBS-MSG-test117",
        prepared_by_user_id=1,
        status="prepared",
        metadata_json={"lead_name": "117", "lead_url": "https://kommo.example/117"},
        sent_confirmed_at=None,
        delivery_error=None,
        metadata_json_set=None,
    )

    actor = SimpleNamespace(
        id=1,
        telegram_user_id=100,
        telegram_username="mgr",
        display_name="Manager",
        status="active",
        role="manager",
    )

    db = AsyncMock()
    db.get = AsyncMock(return_value=actor)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with (
        patch.object(
            client_message_service,
            "_actor",
            AsyncMock(return_value=actor),
        ),
        patch.object(
            client_message_service,
            "get_draft",
            AsyncMock(return_value=record),
        ),
        patch.object(
            client_message_service.kommo_service,
            "get_recent_common_notes",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            client_message_service.kommo_service,
            "add_common_note",
            AsyncMock(),
        ) as add_note,
        patch.object(
            client_message_service.project_link_service,
            "get_by_kommo_lead_id",
            AsyncMock(
                return_value=SimpleNamespace(
                    notion_project_page_id="proj-117",
                    notion_project_url="https://notion.so/proj-117",
                )
            ),
        ),
        patch(
            "app.agent.notion_gateway.log_manual_whatsapp_send",
            AsyncMock(
                return_value={
                    "id": "comm-1",
                    "url": "https://notion.so/comm-1",
                    "project_page_id": "proj-117",
                }
            ),
        ) as notion_log,
        patch.object(
            client_message_service.audit,
            "record_event",
            AsyncMock(),
        ),
    ):
        result = await client_message_service.confirm_sent(
            db, draft_id=11, telegram_user_id=100
        )

    assert result.status == "sent"
    add_note.assert_awaited_once()
    notion_log.assert_awaited_once()
    assert record.metadata_json["notion_communication_id"] == "comm-1"
    text = client_message_service.format_sent_confirmation(record)
    assert "Notion" in text
    assert "comm-1" in text or "notion.so" in text


@pytest.mark.asyncio
async def test_confirm_sent_soft_fails_notion_without_failing_kommo():
    record = SimpleNamespace(
        id=12,
        kommo_lead_id=117001,
        client_name="Ewa Stokowska",
        body="Szanowna Pani Ewo...",
        communication_language="pl",
        delivery_marker="BBS-MSG-test118",
        prepared_by_user_id=1,
        status="prepared",
        metadata_json={"lead_name": "117"},
        sent_confirmed_at=None,
        delivery_error=None,
    )
    actor = SimpleNamespace(
        id=1,
        telegram_user_id=100,
        telegram_username="mgr",
        display_name="Manager",
        status="active",
        role="manager",
    )
    db = AsyncMock()
    db.get = AsyncMock(return_value=actor)
    db.commit = AsyncMock()

    with (
        patch.object(
            client_message_service, "_actor", AsyncMock(return_value=actor)
        ),
        patch.object(
            client_message_service, "get_draft", AsyncMock(return_value=record)
        ),
        patch.object(
            client_message_service.kommo_service,
            "get_recent_common_notes",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            client_message_service.kommo_service,
            "add_common_note",
            AsyncMock(),
        ),
        patch.object(
            client_message_service.project_link_service,
            "get_by_kommo_lead_id",
            AsyncMock(return_value=None),
        ),
        patch(
            "app.agent.notion_gateway.log_manual_whatsapp_send",
            AsyncMock(side_effect=RuntimeError("Notion HTTP 400: bad select")),
        ),
        patch.object(
            client_message_service.audit, "record_event", AsyncMock()
        ),
    ):
        result = await client_message_service.confirm_sent(
            db, draft_id=12, telegram_user_id=100
        )

    assert result.status == "sent"
    assert "Notion HTTP 400" in str(record.metadata_json.get("notion_sync_error"))
    text = client_message_service.format_sent_confirmation(record)
    assert "не обновлён" in text


@pytest.mark.asyncio
async def test_log_manual_whatsapp_send_builds_outbound_communication():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from app.agent import notion_gateway

    with (
        patch.object(notion_gateway.settings, "notion_api_token", "secret"),
        patch.object(
            notion_gateway.settings,
            "notion_communications_data_source_id",
            "ds-comm",
        ),
        patch.object(
            notion_gateway,
            "resolve_project_page_id",
            AsyncMock(return_value="proj-1"),
        ),
        patch.object(
            notion_gateway,
            "create_project_communication",
            AsyncMock(return_value={"id": "c1", "url": "https://notion.so/c1"}),
        ) as create_comm,
        patch.object(
            notion_gateway,
            "touch_project_last_contact",
            AsyncMock(return_value={"id": "proj-1"}),
        ) as touch,
    ):
        result = await notion_gateway.log_manual_whatsapp_send(
            lead={"id": 117001, "name": "117 — Ewa"},
            body="Szanowna Pani Ewo",
            language="pl",
            sender_label="Manager",
            delivery_marker="BBS-MSG-x",
            project_page_id="proj-1",
            occurred_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
        )

    assert result["id"] == "c1"
    assert result["project_page_id"] == "proj-1"
    kwargs = create_comm.await_args.kwargs
    assert kwargs["channel"] == "WhatsApp"
    assert kwargs["communication_type"] == "Исходящее"
    assert "BBS-MSG-x" in kwargs["summary"]
    touch.assert_awaited_once()


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
