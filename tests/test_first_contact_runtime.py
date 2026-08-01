from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import memory, project_snapshot, service as agent_service, tools
from app.api import telegram as telegram_api
from app.services import (
    client_language_service,
    client_message_service,
    first_contact_runtime,
    kommo_service,
    telegram_service,
    unified_project_service,
)


def _all_buttons(markup):
    return [
        button
        for row in markup.get("inline_keyboard") or []
        for button in row
    ]


def test_first_contact_button_is_available_in_every_lead_card():
    first_contact_runtime.install_first_contact_runtime()

    snapshot = project_snapshot.ProjectSnapshot(identity={"kommo_lead_id": 77})
    project = unified_project_service.UnifiedProject(kommo_lead_id=77)
    lead = {"id": 77, "name": "Produkty rolne"}

    for markup in (
        project_snapshot.project_actions_markup(snapshot),
        unified_project_service.project_actions_markup(project),
        tools.lead_card_actions_markup(lead),
    ):
        buttons = _all_buttons(markup)
        callbacks = {button.get("callback_data") for button in buttons}
        labels = {button.get("text") for button in buttons}
        assert "agent:prep:first:77" in callbacks
        assert "agent:prep:draft:77" in callbacks
        assert "👋 Первый контакт" in labels


def test_first_contact_validation_rejects_followup_wording():
    issues = first_contact_runtime._first_contact_issues(
        "Dzień dobry Panie Marcinie, dziękuję za dzisiejszą rozmowę. "
        "Jak ustaliliśmy, wracam do tematu."
    )
    assert any("предыдущий контакт" in issue for issue in issues)


def test_first_contact_context_does_not_use_message_history():
    context = first_contact_runtime._first_contact_context(
        {
            "id": 77,
            "name": "Produkty rolne",
            "conversation": [
                {"direction": "incoming", "text": "Stara wiadomość klienta"}
            ],
        },
        "Подготовь первое сообщение",
    )
    assert context["interaction_mode"] == "first_contact"
    assert context["conversation"]["available"] is False
    assert context["conversation"]["last_messages"] == []
    assert context["conversation"]["ignored_for_first_contact"] is True


@pytest.mark.asyncio
async def test_first_contact_callback_creates_dedicated_whatsapp_draft():
    first_contact_runtime.install_first_contact_runtime()
    db = AsyncMock()
    session = SimpleNamespace(context={})
    lead = {
        "id": 77,
        "name": "169 - Produkty rolne",
        "url": "https://kommo.test/77",
        "contacts": [
            {
                "id": 5,
                "name": "Marcin Bojdo",
                "phones": ["+48519392197"],
                "emails": ["marcin@example.pl"],
            }
        ],
        "custom_fields": {"Produkt": "Produkty rolne"},
        "notes": [],
    }
    resolution = SimpleNamespace(
        language="pl",
        source="market_fallback",
        client_id=None,
    )
    generated = {
        "body": "Dzień dobry Panie Marcinie, piszę w sprawie zapytania dotyczącego produktów rolnych.",
        "language": "pl",
        "kind": first_contact_runtime.FIRST_CONTACT_KIND,
    }
    record = SimpleNamespace(
        id=91,
        communication_language="pl",
        language_source="market_fallback",
        metadata_json={"draft_kind": first_contact_runtime.FIRST_CONTACT_KIND},
    )

    with (
        patch.object(memory, "get_or_create_session", new=AsyncMock(return_value=session)),
        patch.object(memory, "build_context", new=AsyncMock(return_value={})),
        patch.object(memory, "set_active_lead", new=AsyncMock()),
        patch.object(memory, "update_context", new=AsyncMock()) as update_context,
        patch.object(kommo_service, "get_lead_details", new=AsyncMock(return_value=lead)),
        patch.object(
            client_language_service,
            "resolve_communication_language",
            new=AsyncMock(return_value=resolution),
        ),
        patch.object(
            first_contact_runtime.generation,
            "generate_draft",
            new=AsyncMock(return_value=generated),
        ) as generate_draft,
        patch.object(
            client_message_service,
            "create_client_message_draft",
            new=AsyncMock(return_value=record),
        ) as create_draft,
        patch.object(
            client_message_service,
            "format_client_message_draft",
            return_value="preview",
        ),
        patch.object(
            client_message_service,
            "message_draft_markup",
            return_value={"inline_keyboard": []},
        ),
    ):
        reply = await agent_service.handle_callback(
            db,
            callback_data="agent:prep:first:77",
            telegram_user_id=100,
            chat_id=200,
        )

    assert reply is not None
    assert reply.intent == "generate_first_contact"
    assert reply.metadata["draft_kind"] == first_contact_runtime.FIRST_CONTACT_KIND
    assert generate_draft.await_args.kwargs["kind"] == first_contact_runtime.FIRST_CONTACT_KIND
    assert generate_draft.await_args.kwargs["language"] == "pl"
    assert create_draft.await_args.kwargs["channel"] == "whatsapp"
    assert create_draft.await_args.kwargs["draft"]["kind"] == first_contact_runtime.FIRST_CONTACT_KIND
    update_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_language_switch_preserves_first_contact_mode():
    first_contact_runtime.install_first_contact_runtime()
    db = AsyncMock()
    record = SimpleNamespace(
        id=91,
        kommo_lead_id=77,
        body="Dzień dobry Panie Marcinie",
        metadata_json={"draft_kind": first_contact_runtime.FIRST_CONTACT_KIND},
    )
    updated = SimpleNamespace(
        id=91,
        communication_language="uk",
        metadata_json={"draft_kind": first_contact_runtime.FIRST_CONTACT_KIND},
    )
    lead = {"id": 77, "name": "Produkty rolne", "contacts": [], "notes": []}
    generated = {
        "body": "Добрий день, пане Марціне",
        "language": "uk",
        "kind": first_contact_runtime.FIRST_CONTACT_KIND,
    }

    with (
        patch.object(
            client_message_service,
            "get_draft",
            new=AsyncMock(return_value=record),
        ),
        patch.object(
            telegram_api.identity_service,
            "current_user",
            return_value=SimpleNamespace(role="owner"),
        ),
        patch.object(
            telegram_api.identity_service,
            "can_write",
            return_value=True,
        ),
        patch.object(kommo_service, "get_lead_details", new=AsyncMock(return_value=lead)),
        patch.object(
            first_contact_runtime.generation,
            "generate_draft",
            new=AsyncMock(return_value=generated),
        ) as generate_draft,
        patch.object(
            client_message_service,
            "update_language_and_body",
            new=AsyncMock(return_value=updated),
        ) as update_language,
        patch.object(
            client_message_service,
            "format_client_message_draft",
            return_value="preview",
        ),
        patch.object(
            client_message_service,
            "message_draft_markup",
            return_value={"inline_keyboard": []},
        ),
        patch.object(telegram_service, "send_message", new=AsyncMock()) as send_message,
    ):
        handled = await telegram_api._handle_client_message_callback(
            callback_data="clientmsg:lang:uk:91",
            chat_id=200,
            user_id=100,
            db=db,
        )

    assert handled is True
    assert generate_draft.await_args.kwargs["kind"] == first_contact_runtime.FIRST_CONTACT_KIND
    assert generate_draft.await_args.kwargs["language"] == "uk"
    assert "предыдущий разговор" in generate_draft.await_args.kwargs["manager_request"]
    update_language.assert_awaited_once()
    send_message.assert_awaited_once()
