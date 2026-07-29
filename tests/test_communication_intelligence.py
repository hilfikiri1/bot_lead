from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent import generation
from app.services import (
    communication_context_service,
    communication_example_service,
    message_review_service,
    runtime_extensions,
)


def test_playbook_has_source_priority_and_legal_boundaries():
    playbook = communication_context_service.load_communication_playbook()
    assert playbook["version"] == communication_context_service.KNOWLEDGE_VERSION
    assert playbook["priority_order"][0] == "latest_client_conversation"
    assert playbook["legal_boundaries"]
    assert any("Never invent" in rule for rule in playbook["global_rules"])


def test_communication_context_uses_real_chat_and_detects_waiting_on_us():
    lead = {
        "id": 118,
        "name": "118 - Карнизы",
        "status_name": "Первый контакт",
        "custom_fields": {"product": "karnisze", "budget": "10 000 USD"},
        "notes": [
            {"text": "Внутренняя заметка: не отправлять клиенту."},
            {
                "text": "[BBS-MSG-demo]\nWhatsApp-сообщение отправлено.\n\n"
                "Текст:\nDzień dobry, proszę przesłać zdjęcie profilu.",
                "created_at": 10,
            },
        ],
        "chat_context": {
            "origin": "facebook",
            "messages": [
                {
                    "id": "1",
                    "direction": "outgoing",
                    "text": "Jutro na pewno się z Panią skontaktuję.",
                    "created_at": 20,
                },
                {
                    "id": "2",
                    "direction": "incoming",
                    "text": "Dziękuję, czekam. Czy zadzwoni Pan po 14:00?",
                    "created_at": 30,
                },
            ],
            "analysis": {
                "waiting_on": "us",
                "recommended_action": "Позвонить клиенту",
            },
        },
    }
    result = communication_context_service.build_communication_context(
        lead, manager_request="Подготовь ответ"
    )
    conversation = result["conversation"]
    assert conversation["waiting_on"] == "us"
    assert "po 14:00" in conversation["last_client_message"]
    assert conversation["last_manager_message"]
    assert conversation["open_questions"]
    assert any("skontaktuję" in value for value in conversation["promises_made"])
    all_text = " ".join(item["text"] for item in conversation["last_messages"])
    assert "Внутренняя заметка" not in all_text


def test_similar_examples_prefers_polish_vehicle_correction():
    result = communication_example_service.find_similar_examples(
        kind="followup_message",
        language="pl",
        channel="whatsapp",
        query="Lexus BMW Toyota Chiny produkcja homologacja samochód plug-in",
        limit=3,
    )
    assert result
    assert result[0]["id"] == "pl_vehicle_market_correction_001"
    assert "approved_reply" in result[0]
    assert "@" not in result[0]["approved_reply"]


def test_deterministic_reviewer_flags_placeholder_and_overpromise():
    result = message_review_service.deterministic_review(
        body=(
            "Szanowna Pani, gwarantujemy jakość i na pewno dostarczymy towar. "
            "Z poważaniem, [Twoje Imię]"
        ),
        kind="followup_message",
        language="pl",
        playbook={"reviewer_checks": ["placeholder", "guarantee"]},
    )
    assert result["approved"] is False
    assert any("szablon" in issue.lower() for issue in result["issues"])
    assert any("gwaranc" in issue.lower() for issue in result["issues"])


def test_runtime_metadata_preserves_original_reviewed_and_models():
    metadata = runtime_extensions._communication_intelligence_metadata(
        {
            "body": "Final",
            "ai_original_body": "Original",
            "reviewed_body": "Reviewed",
            "review_approved": False,
            "review_issues": ["Issue"],
            "knowledge_version": "v1",
            "writer_model": "writer",
            "reviewer_model": "reviewer",
            "generation_context": {"waiting_on": "us"},
        }
    )
    assert metadata["ai_original_body"] == "Original"
    assert metadata["reviewed_body"] == "Reviewed"
    assert metadata["manager_final_body"] is None
    assert metadata["review_issues"] == ["Issue"]
    assert metadata["generation_context"]["waiting_on"] == "us"


@pytest.mark.asyncio
async def test_generate_draft_includes_chat_playbook_examples_and_review_metadata(
    monkeypatch,
):
    writer_payloads: list[dict] = []

    class FakeCompletions:
        async def create(self, **kwargs):
            writer_payloads.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "title": "Follow-up",
                                    "subject": None,
                                    "body": "Dzień dobry, czy projekt jest nadal aktualny?",
                                    "missing_data": [],
                                    "assumptions": [],
                                    "next_action": "Wait for reply",
                                    "language": "Polish",
                                },
                                ensure_ascii=False,
                            )
                        )
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(generation, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(generation.settings, "openai_api_key", "test-key")
    monkeypatch.setattr(generation.settings, "agent_writer_model", "writer-test")
    monkeypatch.setenv("AGENT_MESSAGE_REVIEWER_ENABLED", "false")

    result = await generation.generate_draft(
        kind="followup_message",
        language="pl",
        manager_request="Napisz krótki follow-up o samochodach z Chin.",
        lead={
            "id": 127,
            "name": "127 - Samochody",
            "status_name": "Получен ответ",
            "chat_context": {
                "origin": "facebook",
                "messages": [
                    {
                        "direction": "incoming",
                        "text": "Proszę o ofertę na hybrydę.",
                        "created_at": 1,
                    }
                ],
                "analysis": {"waiting_on": "us"},
            },
        },
    )

    assert result["body"]
    assert result["ai_original_body"] == result["reviewed_body"]
    assert result["knowledge_version"] == "2026-07-29-v1"
    assert result["writer_model"] == "writer-test"
    assert result["generation_context"]["waiting_on"] == "us"
    assert result["generation_context"]["example_ids"]

    payload = json.loads(writer_payloads[0]["messages"][1]["content"])
    assert payload["communication_context"]["conversation"]["last_client_message"]
    assert payload["bbs_playbook"]["global_rules"]
    assert payload["approved_similar_examples"]
