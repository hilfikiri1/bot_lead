from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

from app.services import (
    kommo_chat_service,
    lead_status_sync_service,
    whatsapp_webhook_service,
)


def test_parse_internal_number_accepts_historical_title_without_dash():
    assert (
        lead_status_sync_service.parse_internal_number(
            "68 Пилы, алмазные головки"
        )
        == "68"
    )
    assert lead_status_sync_service.parse_internal_number("68 - Пилы") == "68"
    assert lead_status_sync_service.parse_internal_number("Просьба о контакте") is None


def test_whatsapp_signature_verification():
    body = b'{"entry":[]}'
    secret = "test-secret"
    signature = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    with patch.dict("os.environ", {"WHATSAPP_APP_SECRET": secret}):
        assert whatsapp_webhook_service.verify_meta_signature(body, signature) is True
        assert (
            whatsapp_webhook_service.verify_meta_signature(body, "sha256=bad")
            is False
        )
    with patch.dict("os.environ", {}, clear=True):
        assert whatsapp_webhook_service.verify_meta_signature(body, signature) is False


def test_extract_whatsapp_text_message():
    raw_message = {
        "id": "wamid.1",
        "from": "48600100200",
        "timestamp": "1785320000",
        "type": "text",
        "text": {"body": "Proszę o ofertę"},
    }
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "123"},
                            "contacts": [
                                {
                                    "wa_id": "48600100200",
                                    "profile": {"name": "Jan Kowalski"},
                                }
                            ],
                            "messages": [raw_message],
                        }
                    }
                ]
            }
        ]
    }
    messages = whatsapp_webhook_service.extract_incoming_messages(payload)
    assert messages == [
        {
            "message_id": "wamid.1",
            "phone": "48600100200",
            "name": "Jan Kowalski",
            "message_type": "text",
            "text": "Proszę o ofertę",
            "timestamp": 1785320000,
            "phone_number_id": "123",
            "display_phone_number": None,
            "context_message_id": None,
            "media": None,
            "raw": raw_message,
        }
    ]


def test_chat_analysis_knows_who_should_answer():
    incoming = kommo_chat_service._analyse(
        [{"direction": "incoming", "origin": "facebook", "created_at": None}]
    )
    outgoing = kommo_chat_service._analyse(
        [{"direction": "outgoing", "origin": "whatsapp", "created_at": None}]
    )
    assert incoming["waiting_on"] == "us"
    assert "Ответить" in incoming["recommended_action"]
    assert outgoing["waiting_on"] == "client"
