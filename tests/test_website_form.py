from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def _website_payload() -> dict:
    return {
        "formType": "contact",
        "name": "Jan Kowalski",
        "email": "jan@firma.pl",
        "phone": "+48 783 232 971",
        "company": "Firma Sp. z o.o.",
        "topic": "full",
        "description": "Import linii opakowaniowej",
        "pageUrl": "https://example.com/pl/kontakt",
        "language": "pl",
        "submittedAt": "2026-08-07T10:00:00Z",
    }


def test_website_form_webhook_disabled_without_secret(monkeypatch):
    from app.api import website_form

    monkeypatch.setattr(website_form.settings, "website_lead_webhook_secret", "")
    with pytest.raises(HTTPException) as exc:
        website_form.require_website_form_secret(None)
    assert exc.value.status_code == 503


def test_website_form_webhook_rejects_wrong_secret(monkeypatch):
    from app.api import website_form

    monkeypatch.setattr(website_form.settings, "website_lead_webhook_secret", "correct")
    with pytest.raises(HTTPException) as exc:
        website_form.require_website_form_secret("wrong")
    assert exc.value.status_code == 401


def test_format_website_form_message_includes_core_fields():
    from app.services.website_form_service import format_website_form_message

    message = format_website_form_message(_website_payload())

    assert "Jan Kowalski" in message
    assert "jan@firma.pl" in message
    assert "Import linii opakowaniowej" in message
    assert "Запрос с сайта" in message


def test_website_lead_title_and_note_are_readable():
    from app.services.website_form_service import (
        build_website_lead_title,
        format_website_form_note,
    )

    payload = _website_payload()
    title = build_website_lead_title(payload)
    note = format_website_form_note(payload)

    assert title == "WWWPL - Import linii opakowaniowej"
    assert "ЗАЯВКА С САЙТА BUY & BRING SOLUTIONS" in note
    assert "Firma Sp. z o.o." in note
    assert "Источник: website_form" in note


def test_website_lead_title_matches_contact_request_without_client_name():
    from app.services.website_form_service import (
        build_website_lead_title,
        format_website_form_note,
    )

    payload = {
        **_website_payload(),
        "name": "Kyrylo",
        "email": "s.poland.s@mail.ru",
        "phone": "+48 555 555 555",
        "company": "Podolskyi",
        "description": "Pomooocyyy",
    }

    assert build_website_lead_title(payload) == "WWWPL - Pomooocyyy"
    note = format_website_form_note(payload)
    for expected in (
        "Kyrylo",
        "s.poland.s@mail.ru",
        "+48 555 555 555",
        "Podolskyi",
        "Kompleksowa obsługa importu",
        "Pomooocyyy",
    ):
        assert expected in note


@pytest.mark.asyncio
async def test_external_intake_creates_contact_lead_and_note(monkeypatch):
    from app.services import kommo_service

    monkeypatch.setattr(kommo_service.settings, "kommo_base_url", "https://bbs.kommo.com")
    monkeypatch.setattr(
        kommo_service,
        "_resolve_configured_lead_placement",
        AsyncMock(return_value={"pipeline_id": 7, "status_id": 8}),
    )
    monkeypatch.setattr(
        kommo_service,
        "find_existing_contact",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        kommo_service,
        "get_contact_field_ids",
        AsyncMock(return_value={"PHONE": 10, "EMAIL": 11}),
    )
    monkeypatch.setattr(
        kommo_service,
        "find_existing_company",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        kommo_service,
        "get_lead_custom_fields",
        AsyncMock(
            return_value=[
                {"id": 20, "name": "Имя", "type": "text"},
                {"id": 21, "name": "E-mail", "type": "text"},
                {"id": 22, "name": "Номер телефона", "type": "text"},
                {"id": 23, "name": "Компания", "type": "text"},
                {"id": 24, "name": "Тема / услуга", "type": "text"},
                {"id": 25, "name": "Описание заявки", "type": "textarea"},
            ]
        ),
    )
    submit = AsyncMock(
        return_value=[
            {"id": 42, "contact_id": 84, "pipeline_id": 7, "status_id": 8}
        ]
    )
    monkeypatch.setattr(kommo_service, "_submit_new_lead", submit)
    add_note = AsyncMock(return_value=True)
    monkeypatch.setattr(kommo_service, "add_common_note", add_note)
    monkeypatch.setattr(
        kommo_service,
        "get_pipeline_index",
        AsyncMock(return_value=({7: "Clients"}, {(7, 8): "New lead"})),
    )

    result = await kommo_service.create_lead_from_external_intake(
        lead_title="WWWPL - Import linii opakowaniowej",
        client_data={
            "name": "Jan Kowalski",
            "company": "Firma Sp. z o.o.",
            "phone": "+48 783 232 971",
            "email": "jan@firma.pl",
        },
        lead_fields={
            "name": "Jan Kowalski",
            "company": "Firma Sp. z o.o.",
            "phone": "+48 783 232 971",
            "email": "jan@firma.pl",
            "topic": "Kompleksowa obsługa importu",
            "description": "Import linii opakowaniowej",
        },
        note_text="SOURCE NOTE",
    )

    lead_payload = submit.await_args.args[0]
    assert submit.await_args.args[1] == "/api/v4/leads/complex"
    assert lead_payload["pipeline_id"] == 7
    contact = lead_payload["_embedded"]["contacts"][0]
    assert contact["name"] == "Jan Kowalski"
    assert {field["field_id"] for field in contact["custom_fields_values"]} == {10, 11}
    assert lead_payload["_embedded"]["companies"] == [{"name": "Firma Sp. z o.o."}]
    assert {field["field_id"] for field in lead_payload["custom_fields_values"]} == {
        20,
        21,
        22,
        23,
        24,
        25,
    }
    add_note.assert_awaited_once_with(42, "SOURCE NOTE")
    assert result["lead_id"] == 42
    assert result["contact_id"] == 84
    assert result["pipeline_name"] == "Clients"
    assert result["status_name"] == "New lead"
    assert result["url"] == "https://bbs.kommo.com/leads/detail/42"


@pytest.mark.asyncio
async def test_external_intake_keeps_created_lead_if_note_fails(monkeypatch):
    from app.services import kommo_service

    monkeypatch.setattr(kommo_service.settings, "kommo_base_url", "https://bbs.kommo.com")
    monkeypatch.setattr(
        kommo_service,
        "_create_lead_with_contact",
        AsyncMock(return_value=({"id": 42}, 84, {"name": "WWW PL — Jan"})),
    )
    monkeypatch.setattr(
        kommo_service,
        "add_common_note",
        AsyncMock(side_effect=RuntimeError("note down")),
    )
    monkeypatch.setattr(
        kommo_service,
        "get_pipeline_index",
        AsyncMock(return_value=({}, {})),
    )

    result = await kommo_service.create_lead_from_external_intake(
        lead_title="WWW PL — Jan",
        client_data={"name": "Jan"},
        note_text="SOURCE NOTE",
    )

    assert result["lead_id"] == 42
    assert result["contact_id"] == 84
    assert result["note_saved"] is False


@pytest.mark.asyncio
async def test_website_form_sync_passes_structured_lead_fields(monkeypatch):
    from app.services import website_form_service

    create = AsyncMock(return_value={"lead_id": 42})
    monkeypatch.setattr(
        website_form_service.kommo_service,
        "create_lead_from_external_intake",
        create,
    )

    await website_form_service.sync_website_form_to_kommo(_website_payload())

    kwargs = create.await_args.kwargs
    assert kwargs["lead_title"] == "WWWPL - Import linii opakowaniowej"
    assert kwargs["lead_fields"]["name"] == "Jan Kowalski"
    assert kwargs["lead_fields"]["company"] == "Firma Sp. z o.o."
    assert kwargs["lead_fields"]["topic"] == "Kompleksowa obsługa importu"
    assert kwargs["lead_fields"]["description"] == "Import linii opakowaniowej"


@pytest.mark.asyncio
async def test_telegram_notification_contains_kommo_link_and_button(monkeypatch):
    from app.services import website_form_service

    monkeypatch.setattr(website_form_service.settings, "telegram_approval_chat_id", 123)
    send_message = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        website_form_service.telegram_service,
        "send_message",
        send_message,
    )

    await website_form_service.notify_website_form(
        _website_payload(),
        kommo_result={
            "lead_id": 42,
            "url": "https://bbs.kommo.com/leads/detail/42",
        },
    )

    assert send_message.await_args.args[0] == 123
    assert "сделка #42 создана" in send_message.await_args.args[1]
    assert send_message.await_args.kwargs["reply_markup"] == {
        "inline_keyboard": [
            [
                {
                    "text": "Открыть в Kommo",
                    "url": "https://bbs.kommo.com/leads/detail/42",
                }
            ]
        ]
    }


@pytest.mark.asyncio
async def test_delivery_keeps_telegram_as_fallback_when_kommo_fails(monkeypatch):
    from app.services import website_form_service

    monkeypatch.setattr(
        website_form_service,
        "sync_website_form_to_kommo",
        AsyncMock(side_effect=RuntimeError("kommo down")),
    )
    notify = AsyncMock()
    monkeypatch.setattr(website_form_service, "notify_website_form", notify)

    result = await website_form_service.deliver_website_form(_website_payload())

    assert result == {"kommo": False, "telegram": True, "lead_id": None}
    assert notify.await_args.kwargs["kommo_failed"] is True


@pytest.mark.asyncio
async def test_delivery_keeps_kommo_when_telegram_fails(monkeypatch):
    from app.services import website_form_service

    monkeypatch.setattr(
        website_form_service,
        "sync_website_form_to_kommo",
        AsyncMock(return_value={"lead_id": 42, "url": "https://bbs.kommo.com/leads/detail/42"}),
    )
    monkeypatch.setattr(
        website_form_service,
        "notify_website_form",
        AsyncMock(side_effect=RuntimeError("telegram down")),
    )

    result = await website_form_service.deliver_website_form(_website_payload())

    assert result == {"kommo": True, "telegram": False, "lead_id": 42}


@pytest.mark.asyncio
async def test_delivery_fails_only_when_both_destinations_fail(monkeypatch):
    from app.services import website_form_service

    monkeypatch.setattr(
        website_form_service,
        "sync_website_form_to_kommo",
        AsyncMock(side_effect=RuntimeError("kommo down")),
    )
    monkeypatch.setattr(
        website_form_service,
        "notify_website_form",
        AsyncMock(side_effect=RuntimeError("telegram down")),
    )

    with pytest.raises(RuntimeError, match="Website lead delivery failed"):
        await website_form_service.deliver_website_form(_website_payload())
