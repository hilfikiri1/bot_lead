from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from starlette.datastructures import UploadFile


def qualified_payload() -> dict:
    return {
        "formType": "contact",
        "name": "Jan Kowalski",
        "email": "jan@firma.pl",
        "phone": "+48 783 232 971",
        "company": "Firma Sp. z o.o.",
        "topic": "full",
        "product": "Generator gazowy 250 kW",
        "quantity": "4 szt.",
        "budget": "20 000+ USD",
        "destination": "Polska, Poznań",
        "deadline": "listopad 2026",
        "description": "Potrzebujemy kompletnej oferty.",
        "pageUrl": "https://example.com/pl/kontakt",
        "language": "pl",
        "submittedAt": "2026-08-08T10:00:00Z",
    }


def test_qualified_fields_are_visible_in_note_message_and_title():
    from app.services.website_form_service import (
        build_website_lead_title,
        format_website_form_message,
        format_website_form_note,
    )

    payload = qualified_payload()
    assert build_website_lead_title(payload) == "WWWPL - Generator gazowy 250 kW"

    note = format_website_form_note(payload)
    message = format_website_form_message(payload)
    for expected in (
        "Generator gazowy 250 kW",
        "4 szt.",
        "20 000+ USD",
        "Polska, Poznań",
        "listopad 2026",
        "Potrzebujemy kompletnej oferty.",
    ):
        assert expected in note
        assert expected in message


@pytest.mark.asyncio
async def test_kommo_description_contains_qualification_details(monkeypatch):
    from app.services import website_form_service

    create = AsyncMock(return_value={"lead_id": 42})
    monkeypatch.setattr(
        website_form_service.kommo_service,
        "create_lead_from_external_intake",
        create,
    )

    await website_form_service.sync_website_form_to_kommo(qualified_payload())

    description = create.await_args.kwargs["lead_fields"]["description"]
    assert "Продукт: Generator gazowy 250 kW" in description
    assert "Количество: 4 szt." in description
    assert "Бюджет проекта: 20 000+ USD" in description
    assert "Доставка: Polska, Poznań" in description
    assert "Желаемый срок: listopad 2026" in description
    assert "Описание: Potrzebujemy kompletnej oferty." in description


@pytest.mark.asyncio
async def test_telegram_notification_sends_uploaded_document(monkeypatch):
    from app.services import website_form_service

    monkeypatch.setattr(website_form_service.settings, "telegram_approval_chat_id", 123)
    send_message = AsyncMock(return_value={"ok": True})
    send_document = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(website_form_service.telegram_service, "send_message", send_message)
    monkeypatch.setattr(website_form_service.telegram_service, "send_document", send_document)

    await website_form_service.notify_website_form(
        qualified_payload(),
        attachments=[
            {
                "filename": "spec.pdf",
                "content_type": "application/pdf",
                "content": b"pdf-bytes",
            }
        ],
    )

    send_message.assert_awaited_once()
    send_document.assert_awaited_once_with(
        123,
        filename="spec.pdf",
        content=b"pdf-bytes",
        caption="📎 Файл из заявки: spec.pdf",
        mime_type="application/pdf",
    )


@pytest.mark.asyncio
async def test_attachment_reader_accepts_allowed_files():
    from app.api.website_form import _read_attachments

    upload = UploadFile(
        filename="spec.pdf",
        file=BytesIO(b"test"),
        headers={"content-type": "application/pdf"},
    )
    attachments = await _read_attachments([upload])
    assert attachments == [
        {
            "filename": "spec.pdf",
            "content_type": "application/pdf",
            "content": b"test",
        }
    ]
