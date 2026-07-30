"""Strict AI response validation, repair, and rejection (mandatory case 28)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.lead_intake import ai_service
from app.services.lead_intake.schema import LeadQualification, LeadQualificationError

VALID_PAYLOAD = {
    "product_name_ru": "Инструменты",
    "potential": "medium",
    "readiness": "low",
    "priority": "C",
    "priority_label_ru": "квалификация",
    "recommended_action": "whatsapp",
    "recommended_action_reason_ru": "Категория товара указана слишком широко.",
    "lead_analysis_ru": "Клиент указал только общую категорию инструментов.",
    "main_risks_ru": ["Не указан вид инструментов"],
    "missing_information_ru": ["Перечень товаров"],
    "next_steps_ru": ["Отправить сообщение в WhatsApp"],
    "client_message": {"language": "pl", "channel": "whatsapp", "text": "Dzień dobry Panie Andrzeju"},
    "call_script": None,
    "kommo_note_ru": "Полный текст примечания",
    "task": {"type": "follow_up", "title_ru": "Получить перечень инструментов", "due_rule": "next_business_day", "due_at": None},
    "second_follow_up": {"enabled": True, "days_after_first": 2, "text": "Dzień dobry, wracam"},
}


def _openai_response(payload: dict) -> AsyncMock:
    message = AsyncMock()
    message.content = json.dumps(payload, ensure_ascii=False)
    choice = AsyncMock()
    choice.message = message
    response = AsyncMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_valid_response_is_accepted_on_first_try():
    with (
        patch.object(ai_service.settings, "openai_api_key", "test-key"),
        patch.object(
            ai_service._client.chat.completions,
            "create",
            new=AsyncMock(return_value=_openai_response(VALID_PAYLOAD)),
        ) as create_mock,
    ):
        result = await ai_service.generate_lead_qualification({"client_name": "Andrzej Janka"})
    assert isinstance(result, LeadQualification)
    assert result.priority == "C"
    assert result.recommended_action == "whatsapp"
    assert create_mock.await_count == 1


@pytest.mark.asyncio
async def test_invalid_response_is_repaired_once_then_accepted():
    invalid = dict(VALID_PAYLOAD)
    invalid.pop("priority")  # missing required field
    with (
        patch.object(ai_service.settings, "openai_api_key", "test-key"),
        patch.object(
            ai_service._client.chat.completions,
            "create",
            new=AsyncMock(
                side_effect=[_openai_response(invalid), _openai_response(VALID_PAYLOAD)]
            ),
        ) as create_mock,
    ):
        result = await ai_service.generate_lead_qualification({"client_name": "Andrzej Janka"})
    assert isinstance(result, LeadQualification)
    assert create_mock.await_count == 2


@pytest.mark.asyncio
async def test_twice_invalid_response_raises_and_is_never_returned():
    invalid = dict(VALID_PAYLOAD)
    invalid.pop("priority")
    with (
        patch.object(ai_service.settings, "openai_api_key", "test-key"),
        patch.object(
            ai_service._client.chat.completions,
            "create",
            new=AsyncMock(side_effect=[_openai_response(invalid), _openai_response(invalid)]),
        ),
    ):
        with pytest.raises(LeadQualificationError):
            await ai_service.generate_lead_qualification({"client_name": "Andrzej Janka"})


@pytest.mark.asyncio
async def test_missing_api_key_raises_clean_error_without_network_call():
    with patch.object(ai_service.settings, "openai_api_key", ""):
        with pytest.raises(LeadQualificationError):
            await ai_service.generate_lead_qualification({"client_name": "Andrzej Janka"})


def test_schema_rejects_invalid_priority():
    payload = dict(VALID_PAYLOAD)
    payload["priority"] = "Z"
    with pytest.raises(Exception):
        LeadQualification.model_validate(payload)


def test_schema_rejects_unknown_extra_fields():
    payload = dict(VALID_PAYLOAD)
    payload["unexpected_field"] = "value"
    with pytest.raises(Exception):
        LeadQualification.model_validate(payload)
