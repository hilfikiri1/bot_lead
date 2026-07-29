from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.whatsapp_cloud_message import WhatsAppCloudMessage
from app.services import (
    whatsapp_cloud_runtime,
    whatsapp_cloud_service,
    whatsapp_webhook_service,
)


def _payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {
                                "phone_number_id": "phone-id-1",
                                "display_phone_number": "+48 500 000 000",
                            },
                            "contacts": [
                                {
                                    "wa_id": "48500111222",
                                    "profile": {"name": "Jan Kowalski"},
                                }
                            ],
                            "messages": [
                                {
                                    "id": "wamid.in.1",
                                    "from": "48500111222",
                                    "timestamp": "1785337200",
                                    "type": "document",
                                    "context": {"id": "wamid.out.0"},
                                    "document": {
                                        "id": "media-1",
                                        "mime_type": "application/pdf",
                                        "filename": "specification.pdf",
                                        "caption": "Proszę sprawdzić specyfikację",
                                    },
                                }
                            ],
                            "statuses": [
                                {
                                    "id": "wamid.out.1",
                                    "status": "delivered",
                                    "timestamp": "1785337210",
                                    "recipient_id": "48500111222",
                                    "conversation": {"id": "conv-1"},
                                    "pricing": {"category": "service"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_extract_incoming_message_keeps_media_and_context():
    messages = whatsapp_webhook_service.extract_incoming_messages(_payload())
    assert len(messages) == 1
    item = messages[0]
    assert item["message_id"] == "wamid.in.1"
    assert item["phone"] == "48500111222"
    assert item["name"] == "Jan Kowalski"
    assert item["message_type"] == "document"
    assert item["text"] == "Proszę sprawdzić specyfikację"
    assert item["context_message_id"] == "wamid.out.0"
    assert item["media"]["id"] == "media-1"
    assert item["media"]["filename"] == "specification.pdf"


def test_extract_status_updates():
    statuses = whatsapp_webhook_service.extract_status_updates(_payload())
    assert statuses == [
        {
            "message_id": "wamid.out.1",
            "status": "delivered",
            "timestamp": "1785337210",
            "recipient_id": "48500111222",
            "conversation": {"id": "conv-1"},
            "pricing": {"category": "service"},
            "errors": [],
            "raw": {
                "id": "wamid.out.1",
                "status": "delivered",
                "timestamp": "1785337210",
                "recipient_id": "48500111222",
                "conversation": {"id": "conv-1"},
                "pricing": {"category": "service"},
            },
        }
    ]


def test_meta_signature_fails_closed_and_accepts_valid_signature(monkeypatch):
    body = b'{"object":"whatsapp_business_account"}'
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    assert whatsapp_webhook_service.verify_meta_signature(body, None) is False

    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secret")
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert (
        whatsapp_webhook_service.verify_meta_signature(body, f"sha256={digest}")
        is True
    )
    assert whatsapp_webhook_service.verify_meta_signature(body, "sha256=bad") is False


def test_graph_version_and_runtime_button(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v23.0")
    assert whatsapp_cloud_service.enabled() is True
    assert whatsapp_cloud_service.graph_version() == "v23.0"
    record = SimpleNamespace(
        id=55,
        channel="whatsapp",
        recipient="48500111222",
        status="prepared",
    )
    button = whatsapp_cloud_runtime._cloud_button(record)
    assert button == {
        "text": "📤 Отправить через WhatsApp API",
        "callback_data": "clientmsg:sendapi:55",
    }
    assert len(button["callback_data"].encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_freeform_window_is_24_hours(monkeypatch):
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    async def recent(*args, **kwargs):
        return now - timedelta(hours=23, minutes=59)

    monkeypatch.setattr(whatsapp_cloud_service, "last_incoming_at", recent)
    monkeypatch.delenv("WHATSAPP_ENFORCE_24H_WINDOW", raising=False)
    assert await whatsapp_cloud_service.freeform_window_open(
        SimpleNamespace(), phone="48500111222", at=now
    )

    async def old(*args, **kwargs):
        return now - timedelta(hours=24, seconds=1)

    monkeypatch.setattr(whatsapp_cloud_service, "last_incoming_at", old)
    assert not await whatsapp_cloud_service.freeform_window_open(
        SimpleNamespace(), phone="48500111222", at=now
    )


@pytest.mark.asyncio
async def test_send_text_request_uses_meta_messages_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "messaging_product": "whatsapp",
                "messages": [{"id": "wamid.out.99"}],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return FakeResponse()

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "secret-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "phone-123")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v23.0")
    monkeypatch.setattr(whatsapp_cloud_service.httpx, "AsyncClient", FakeClient)

    result = await whatsapp_cloud_service._send_text_request(
        to="48500111222", body="Dzień dobry"
    )
    assert result["provider_message_id"] == "wamid.out.99"
    assert captured["url"] == "https://graph.facebook.com/v23.0/phone-123/messages"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["json"]["messaging_product"] == "whatsapp"
    assert captured["json"]["type"] == "text"
    assert captured["json"]["text"]["body"] == "Dzień dobry"


def test_model_and_migration_are_registered():
    assert WhatsAppCloudMessage.__tablename__ == "whatsapp_cloud_messages"
    migration = Path("migrations/versions/013_whatsapp_cloud_messages.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "013_whatsapp_cloud_messages"' in migration
    assert 'down_revision = "012_supplier_offer_workspace"' in migration
