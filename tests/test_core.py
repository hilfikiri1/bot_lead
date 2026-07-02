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
    def test_configured_menu_pipeline_prefers_menu_setting(self):
        from app.services import kommo_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(kommo_service.settings, "kommo_menu_pipeline_id", 42)
            mp.setattr(kommo_service.settings, "kommo_default_pipeline_id", 10)
            assert kommo_service.configured_menu_pipeline_id() == 42

    def test_configured_menu_pipeline_falls_back_to_default(self):
        from app.services import kommo_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(kommo_service.settings, "kommo_menu_pipeline_id", None)
            mp.setattr(kommo_service.settings, "kommo_default_pipeline_id", 10)
            assert kommo_service.configured_menu_pipeline_id() == 10

    def test_lead_belongs_to_pipeline(self):
        from app.services.kommo_service import _lead_belongs_to_pipeline

        assert _lead_belongs_to_pipeline({"pipeline_id": 5}, 5) is True
        assert _lead_belongs_to_pipeline({"pipeline_id": 5}, 6) is False
        assert _lead_belongs_to_pipeline({"pipeline_id": 5}, None) is True

    @pytest.mark.asyncio
    async def test_update_kommo_lead_patches_changed_fields(self):
        from app.services import kommo_service

        with patch(
            "app.services.kommo_service._request",
            new=AsyncMock(
                return_value={"_embedded": {"leads": [{"id": 99, "name": "New name"}]}}
            ),
        ) as request_mock, patch(
            "app.services.kommo_service.get_lead_details",
            new=AsyncMock(
                return_value={
                    "name": "New name",
                    "price": 1500,
                    "status_name": "Переговоры",
                    "pipeline_name": "Основная",
                    "url": "https://example.kommo.com/leads/detail/99",
                }
            ),
        ):
            result = await kommo_service.update_kommo_lead(
                99, name="New name", price=1500
            )

        payload = request_mock.await_args.kwargs["json_body"][0]
        assert payload == {"id": 99, "name": "New name", "price": 1500}
        assert result["lead_name"] == "New name"
        assert result["price"] == 1500

    @pytest.mark.asyncio
    async def test_get_all_open_leads_filters_pipeline(self):
        from app.services import kommo_service

        page_one = {
            "_embedded": {
                "leads": [
                    {"id": 1, "name": "A", "pipeline_id": 7, "status_id": 1},
                    {"id": 2, "name": "B", "pipeline_id": 8, "status_id": 1},
                ]
            }
        }
        with patch(
            "app.services.kommo_service._request",
            new=AsyncMock(return_value=page_one),
        ), patch(
            "app.services.kommo_service.get_pipeline_index",
            new=AsyncMock(return_value=({7: "Моя воронка"}, {(7, 1): "Новый"})),
        ), patch(
            "app.services.kommo_service.configured_menu_pipeline_id",
            return_value=7,
        ):
            result = await kommo_service.get_all_open_leads()

        assert result["open_count"] == 1
        assert result["leads"][0]["id"] == 1
        assert result["pipeline_name"] == "Моя воронка"

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

    def test_extract_embedded_items_supports_list_response(self):
        from app.services.kommo_service import _extract_embedded_items

        payload = [{"id": 7711893, "name": "90 Надувная горка"}]
        assert _extract_embedded_items(payload, "leads") == payload

    def test_build_analysis_note_contains_sections(self):
        from app.services.kommo_service import build_analysis_note_text

        text = build_analysis_note_text(
            client_data={"name": "Jan", "company": "Test"},
            lead_data={"product_requested": "Горка", "budget": "1000 EUR"},
            conversation_summary="Клиент интересуется горкой.",
            recommended_next_step="Отправить КП.",
            missing_questions=["Какой размер?"],
        )
        assert "АНАЛИЗ РАЗГОВОРА" in text
        assert "Горка" in text
        assert "СЛЕДУЮЩИЙ ШАГ" in text


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


class TestCalendarHelpers:
    def test_build_ics_contains_alarm(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.services.calendar_service import build_ics_content

        start = datetime(2026, 7, 2, 10, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        end = datetime(2026, 7, 2, 10, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
        uid, ics = build_ics_content(
            title="Созвон",
            description="Проверка",
            start_dt=start,
            end_dt=end,
        )
        assert uid.endswith("@buybringsolutions")
        assert "BEGIN:VALARM" in ics
        assert "TRIGGER:-PT10M" in ics

    def test_create_event_with_fallback_returns_ics_on_error(self):
        from app.services import calendar_service

        with patch(
            "app.services.calendar_service.create_event",
            side_effect=calendar_service.CalendarIntegrationError("auth failed"),
        ):
            result = calendar_service.create_event_with_fallback(
                "Созвон",
                "Описание",
                None,
                15,
            )
        assert result["success"] is False
        assert result["ics_content"]
        assert "BEGIN:VCALENDAR" in result["ics_content"]


class TestNotionHelpers:
    def test_is_configured_requires_token_and_calls_db(self):
        from app.services import notion_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(notion_service.settings, "notion_api_token", "")
            mp.setattr(notion_service.settings, "notion_calls_database_id", "db")
            assert notion_service.is_configured() is False
            mp.setattr(notion_service.settings, "notion_api_token", "secret")
            assert notion_service.is_configured() is True


class TestCommandRouter:
    def test_short_command_hint_detected(self):
        from app.services.command_router_service import _looks_like_command

        assert _looks_like_command("напомни завтра в 10 позвонить клиенту")
        assert _looks_like_command("добавь в календарь созвон завтра в 15:00")
        assert not _looks_like_command("x" * 300)

    @pytest.mark.asyncio
    async def test_long_transcript_defaults_to_analysis(self):
        from app.services.command_router_service import classify_message

        plan = await classify_message("word " * 400, context={})
        assert plan.intent == "analyze_conversation"

    def test_parse_relative_time_tomorrow(self):
        from app.services.command_router_service import _parse_relative_time

        parsed = _parse_relative_time("завтра в 10:00")
        assert parsed is not None
        assert "T10:00" in parsed or "T10:00:00" in parsed

    def test_parse_relative_time_today(self):
        from app.services.command_router_service import _parse_relative_time

        parsed = _parse_relative_time("сегодня в 15:30")
        assert parsed is not None
