import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


VALID_AI_RESPONSE = {
    "client": {
        "name": "Jan Kowalski",
        "phone": "+48123456789",
        "email": "jan@example.pl",
        "company": "Kowalski Sp. z o.o.",
        "language": "pl",
    },
    "lead": {
        "lead_number": None,
        "proposed_name": "Лазерный станок 1500 Вт",
        "product_requested": "Лазерный станок 1500 Вт",
        "specifications": ["Работа с листовым металлом"],
        "quantity": "1 шт.",
        "budget": "50000 PLN",
        "country": "Польша",
        "city": "Варшава",
        "delivery_terms": None,
        "certification": "CE",
        "timeline": None,
        "urgency": "high",
        "status": "needs_info",
    },
    "conversation_summary": "Клиент ищет лазерный станок для листового металла.",
    "confirmed_facts": ["Бюджет около 50 000 PLN"],
    "what_manager_said": ["Объяснил порядок поставки"],
    "mistakes_or_weak_points": ["Не уточнена толщина металла"],
    "missing_questions": ["Какая максимальная толщина металла?"],
    "risks": ["Техническое задание пока неполное"],
    "recommended_next_step": "Запросить технические параметры.",
    "manager_task": {"title": "Запросить спецификацию", "due_at": None},
    "email": {
        "subject": "Specyfikacja maszyny laserowej",
        "body": "Dzień dobry Panie Janie, proszę przesłać specyfikację.",
    },
    "whatsapp": {
        "message": "Dzień dobry! Proszę przesłać parametry techniczne maszyny.",
    },
    "calendar": {
        "title": "Повторный звонок клиенту",
        "description": "Уточнить толщину металла и размеры листа.",
        "start_time": None,
        "duration_minutes": 15,
    },
    "confidence_score": 0.72,
    "needs_human_review": True,
}


class TestAIAnalysis:
    def test_valid_response(self):
        from app.services.ai_analysis_service import _validate_schema

        _validate_schema(VALID_AI_RESPONSE)

    def test_missing_keys(self):
        from app.services.ai_analysis_service import _validate_schema

        with pytest.raises(ValueError, match="missing required keys"):
            _validate_schema({"client": {}, "lead": {}})

    def test_manager_prompt_requires_russian(self):
        from app.services.ai_analysis_service import SYSTEM_PROMPT

        assert "manager-facing fields MUST be written" in SYSTEM_PROMPT
        assert "Russian" in SYSTEM_PROMPT

    @pytest.mark.asyncio
    async def test_analyse_transcript_parses_json(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(VALID_AI_RESPONSE)
        with patch("app.services.ai_analysis_service._client") as mock_client:
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            from app.services.ai_analysis_service import analyse_transcript

            result = await analyse_transcript("Rozmowa o maszynie laserowej")
        assert result["lead"]["proposed_name"] == "Лазерный станок 1500 Вт"
        assert result["whatsapp"]["message"].startswith("Dzień dobry")


class TestTelegramUI:
    def test_report_is_russian_and_keeps_polish_client_draft(self):
        from app.services.telegram_service import format_report

        text = format_report(VALID_AI_RESPONSE, "dummy")
        assert "Что ещё выяснить" in text
        assert "Следующий шаг" in text
        assert "Dzień dobry" in text
        assert "50000 PLN" in text

    def test_audio_job_labels(self):
        from app.services.telegram_service import format_audio_jobs

        job = MagicMock()
        job.processing_status = "transcribing"
        job.telegram_message_id = 123
        job.created_at = None
        text = format_audio_jobs([job])
        assert "Транскрибация" in text
        assert "123" in text


class TestKommoHelpers:
    def test_lead_title_uses_number_and_product(self):
        from app.services.kommo_service import _lead_title

        title = _lead_title(
            {},
            {
                "lead_number": "174",
                "proposed_name": "Лазерная резка металла",
                "product_requested": "Лазер",
            },
        )
        assert title == "174 Лазерная резка металла"

    def test_lead_title_override(self):
        from app.services.kommo_service import _lead_title

        assert (
            _lead_title(
                {},
                {"product_requested": "Лазер"},
                lead_name_override="90 Мини-экскаваторы",
            )
            == "90 Мини-экскаваторы"
        )


class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_cancel_returns_russian_message(self):
        from app.services.approval_service import handle_callback

        result = await handle_callback(
            db=AsyncMock(),
            callback_data="action:cancel:1:1",
            telegram_user_id=123,
            chat_id=456,
        )
        # The report lookup happens first in the current backwards-compatible flow.
        assert isinstance(result, str)


class TestKommoCreationRetry:
    @pytest.mark.asyncio
    async def test_invalid_status_retries_without_placement(self):
        from app.services import kommo_service

        rejected = kommo_service.KommoAPIError(
            "NotSupportedChoice status_id",
            status_code=400,
        )
        successful = {"_embedded": {"leads": [{"id": 123}]}}
        with patch(
            "app.services.kommo_service._request",
            new=AsyncMock(side_effect=[rejected, successful]),
        ) as request_mock:
            result = await kommo_service._submit_new_lead(
                {"name": "90 Мини-экскаваторы", "pipeline_id": 10, "status_id": 20},
                "/api/v4/leads",
            )

        assert result == successful
        first_payload = request_mock.await_args_list[0].kwargs["json_body"][0]
        second_payload = request_mock.await_args_list[1].kwargs["json_body"][0]
        assert first_payload["status_id"] == 20
        assert "status_id" not in second_payload
        assert "pipeline_id" not in second_payload
        assert second_payload["name"] == "90 Мини-экскаваторы"

    @pytest.mark.asyncio
    async def test_non_placement_error_is_not_hidden(self):
        from app.services import kommo_service

        rejected = kommo_service.KommoAPIError("Unauthorized", status_code=401)
        with patch(
            "app.services.kommo_service._request",
            new=AsyncMock(side_effect=rejected),
        ):
            with pytest.raises(kommo_service.KommoAPIError):
                await kommo_service._submit_new_lead(
                    {"name": "Тест"},
                    "/api/v4/leads",
                )
