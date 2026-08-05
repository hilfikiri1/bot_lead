import pytest
from fastapi import HTTPException


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

    message = format_website_form_message(
        {
            "formType": "contact",
            "name": "Jan Kowalski",
            "email": "jan@firma.pl",
            "phone": "+48 783 232 971",
            "company": "Firma Sp. z o.o.",
            "topic": "full",
            "description": "Import linii opakowaniowej",
            "pageUrl": "https://example.com/pl/kontakt",
            "language": "pl",
            "submittedAt": "2025-08-05T10:00:00Z",
        }
    )

    assert "Jan Kowalski" in message
    assert "jan@firma.pl" in message
    assert "Import linii opakowaniowej" in message
    assert "Запрос с сайта" in message
