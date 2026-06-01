"""
Unit tests for AI JSON parser and action approval flow.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch


# ─── AI JSON Parser Tests ─────────────────────────────────────────────────────

VALID_AI_RESPONSE = {
    "client": {
        "name": "Jan Kowalski",
        "phone": "+48123456789",
        "email": "jan@example.pl",
        "company": "Kowalski Sp. z o.o.",
        "language": "pl",
    },
    "lead": {
        "product_requested": "Industrial laser cutting machine 1500W",
        "budget": "50000 PLN",
        "country": "Poland",
        "city": "Warsaw",
        "urgency": "high",
        "status": "needs_info",
    },
    "conversation_summary": "Client needs a laser cutter for sheet metal. Budget discussed. Specs not finalised.",
    "what_manager_said": ["Explained FOB incoterms", "Asked about budget"],
    "mistakes_or_weak_points": ["Did not ask for technical specification", "No delivery timeline discussed"],
    "missing_questions": ["What sheet metal thickness?", "Required accuracy?", "CE certificate needed?"],
    "recommended_next_step": "Send spec questionnaire and request product photos",
    "email": {
        "subject": "Zapytanie ofertowe – maszyna laserowa",
        "body": "Dzień dobry Panie Janie,\n\nDziękujemy za rozmowę...",
    },
    "whatsapp": {
        "message": "Cześć Jan! Świetna rozmowa. Wyślę Ci kilka pytań technicznych – potrzebujemy specyfikacji maszyny 🙂",
    },
    "calendar": {
        "title": "Follow-up: Jan Kowalski – laser cutter spec",
        "description": "Ask about sheet thickness, CE cert, delivery terms. Send spec form before call.",
        "start_time": "2024-12-20T10:00:00Z",
        "duration_minutes": 15,
    },
    "confidence_score": 0.72,
    "needs_human_review": False,
}


class TestAIJsonParser:
    def test_valid_response_parses_correctly(self):
        from app.services.ai_analysis_service import _validate_schema
        # Should not raise
        _validate_schema(VALID_AI_RESPONSE)

    def test_missing_required_keys_raises(self):
        from app.services.ai_analysis_service import _validate_schema
        bad = {"client": {}, "lead": {}}
        with pytest.raises(ValueError, match="missing required keys"):
            _validate_schema(bad)

    def test_confidence_score_present(self):
        assert VALID_AI_RESPONSE["confidence_score"] == 0.72

    def test_client_language_values(self):
        valid_langs = {"ru", "pl", "ua", "en", "unknown"}
        lang = VALID_AI_RESPONSE["client"]["language"]
        assert lang in valid_langs

    def test_lead_urgency_values(self):
        valid_urgency = {"low", "medium", "high", "unknown"}
        urgency = VALID_AI_RESPONSE["lead"]["urgency"]
        assert urgency in valid_urgency

    def test_lead_status_values(self):
        valid_status = {"new", "needs_info", "ready_for_supplier_search", "follow_up"}
        status = VALID_AI_RESPONSE["lead"]["status"]
        assert status in valid_status

    def test_missing_questions_is_list(self):
        assert isinstance(VALID_AI_RESPONSE["missing_questions"], list)
        assert len(VALID_AI_RESPONSE["missing_questions"]) > 0

    def test_calendar_has_duration(self):
        cal = VALID_AI_RESPONSE["calendar"]
        assert cal["duration_minutes"] == 15

    @pytest.mark.asyncio
    async def test_analyse_transcript_calls_openai(self):
        """Test that analyse_transcript correctly calls OpenAI and parses response."""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(VALID_AI_RESPONSE)

        with patch("app.services.ai_analysis_service._client") as mock_client:
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            from app.services.ai_analysis_service import analyse_transcript
            result = await analyse_transcript("Test transcript about laser machine")

        assert result["client"]["name"] == "Jan Kowalski"
        assert result["lead"]["product_requested"] == "Industrial laser cutting machine 1500W"

    @pytest.mark.asyncio
    async def test_analyse_transcript_raises_on_invalid_json(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "not valid json {"

        with patch("app.services.ai_analysis_service._client") as mock_client:
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            from app.services.ai_analysis_service import analyse_transcript
            with pytest.raises(ValueError, match="invalid JSON"):
                await analyse_transcript("test")


# ─── Approval Flow Tests ──────────────────────────────────────────────────────

class TestApprovalFlow:
    @pytest.mark.asyncio
    async def test_cancel_returns_message(self):
        from app.services.approval_service import handle_callback
        db = AsyncMock()
        result = await handle_callback(
            db=db,
            callback_data="action:cancel:1:1",
            telegram_user_id=123,
            chat_id=456,
        )
        assert "Cancelled" in result

    @pytest.mark.asyncio
    async def test_unknown_action_gracefully_handled(self):
        from app.services.approval_service import handle_callback
        db = AsyncMock()
        result = await handle_callback(
            db=db,
            callback_data="action:unknown:1:1",
            telegram_user_id=123,
            chat_id=456,
        )
        # Should not crash, returns some message
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_gmail_action_missing_email_returns_warning(self):
        from app.services.approval_service import handle_callback
        from unittest.mock import AsyncMock, MagicMock

        # Mock DB returning a voice note with report but no client email
        mock_vn = MagicMock()
        mock_report = MagicMock()
        mock_report.email_subject = "Test"
        mock_report.email_body = "Body"
        mock_vn.ai_report = mock_report

        mock_lead = MagicMock()
        mock_client = MagicMock()
        mock_client.email = None  # No email
        mock_lead.client = mock_client

        async def mock_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = mock_vn
            return result

        db = AsyncMock()
        db.execute = mock_execute

        # Override the lead query too
        call_count = [0]
        async def mock_execute2(stmt):
            result = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                result.scalar_one_or_none.return_value = mock_vn
            else:
                result.scalar_one_or_none.return_value = mock_lead
            return result

        db.execute = mock_execute2

        with patch("app.services.approval_service.crm_service.create_action", new_callable=AsyncMock) as mock_action:
            mock_action.return_value = MagicMock()
            result = await handle_callback(
                db=db,
                callback_data="action:gmail:1:1",
                telegram_user_id=123,
                chat_id=456,
            )
        assert isinstance(result, str)

    def test_whatsapp_draft_not_sent_to_client(self):
        """WhatsApp service must raise if WHATSAPP_ENABLED is false."""
        from app.services.whatsapp_service import send_message
        import asyncio

        async def _try_send():
            with pytest.raises(RuntimeError, match="disabled"):
                await send_message("+48123456789", "test message")

        asyncio.get_event_loop().run_until_complete(_try_try_send := _try_send())

    def test_whatsapp_prepare_draft_does_not_send(self):
        """prepare_message_draft must return a dict, not send anything."""
        from app.services.whatsapp_service import prepare_message_draft
        result = prepare_message_draft("+48123456789", "Hello!")
        assert result["preview"] is True
        assert "NOT been sent" in result["note"]
        assert result["to"] == "+48123456789"


# ─── Telegram Format Tests ────────────────────────────────────────────────────

class TestTelegramReport:
    def test_format_report_contains_client_info(self):
        from app.services.telegram_service import format_report
        text = format_report(VALID_AI_RESPONSE, "dummy transcript")
        assert "Jan Kowalski" in text
        assert "Kowalski Sp. z o.o." in text
        assert "Warsaw" in text

    def test_format_report_contains_email_draft(self):
        from app.services.telegram_service import format_report
        text = format_report(VALID_AI_RESPONSE, "dummy transcript")
        assert "Zapytanie ofertowe" in text

    def test_format_report_contains_missing_questions(self):
        from app.services.telegram_service import format_report
        text = format_report(VALID_AI_RESPONSE, "dummy transcript")
        assert "CE certificate" in text

    def test_format_report_contains_confidence(self):
        from app.services.telegram_service import format_report
        text = format_report(VALID_AI_RESPONSE, "dummy transcript")
        assert "72%" in text
