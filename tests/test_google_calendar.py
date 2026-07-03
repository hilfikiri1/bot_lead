"""Tests for Google Calendar integration."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services import calendar_event_builder, calendar_service, google_calendar_service


SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "bbs-bot",
    "private_key_id": "key",
    "private_key": "-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n",
    "client_email": "bot@bbs-bot.iam.gserviceaccount.com",
    "client_id": "123",
    "token_uri": "https://oauth2.googleapis.com/token",
}


class TestServiceAccountParsing:
    def test_parse_json(self):
        with patch.object(
            google_calendar_service.settings,
            "google_service_account_json",
            json.dumps(SERVICE_ACCOUNT),
        ), patch.object(
            google_calendar_service.settings, "google_service_account_json_base64", ""
        ):
            info = google_calendar_service._load_service_account_info()
        assert info["client_email"] == "bot@bbs-bot.iam.gserviceaccount.com"

    def test_parse_base64(self):
        encoded = base64.b64encode(json.dumps(SERVICE_ACCOUNT).encode()).decode()
        with patch.object(google_calendar_service.settings, "google_service_account_json", ""), patch.object(
            google_calendar_service.settings,
            "google_service_account_json_base64",
            encoded,
        ):
            info = google_calendar_service._load_service_account_info()
        assert info["project_id"] == "bbs-bot"

    def test_missing_credentials(self):
        with patch.object(google_calendar_service.settings, "google_service_account_json", ""), patch.object(
            google_calendar_service.settings, "google_service_account_json_base64", ""
        ):
            with pytest.raises(google_calendar_service.GoogleCalendarError):
                google_calendar_service._load_service_account_info()


class TestNaturalLanguageParsing:
    def test_tomorrow_at_ten(self):
        tz = ZoneInfo("Europe/Warsaw")
        now = datetime(2026, 7, 3, 9, 0, tzinfo=tz)
        start, duration = calendar_event_builder.parse_natural_datetime(
            "созвон завтра в 10:00", now=now
        )
        assert start.day == 4
        assert start.hour == 10
        assert duration == 30

    def test_duration_phrase(self):
        tz = ZoneInfo("Europe/Warsaw")
        now = datetime(2026, 7, 3, 9, 0, tzinfo=tz)
        start, duration = calendar_event_builder.parse_natural_datetime(
            "встреча завтра в 15:30 на 60 минут", now=now
        )
        assert start.hour == 15
        assert start.minute == 30
        assert duration == 60


class TestEventBuilder:
    def test_title_with_lead(self):
        title = calendar_event_builder.build_event_title("call", "110 - Игрушки")
        assert title == "Созвон: 110 - Игрушки"

    def test_description_contains_kommo_link(self):
        description = calendar_event_builder.build_event_description(
            lead_name="110 - Игрушки",
            kommo_lead_id=10373783,
            lead_url="https://example.kommo.com/leads/detail/10373783",
            contact_name="Roman",
            contact_phone="+48123456789",
            product_hint="игрушки",
        )
        assert "10373783" in description
        assert "https://example.kommo.com/leads/detail/10373783" in description
        assert "Roman" in description

    def test_idempotency_key_stable(self):
        key1 = calendar_event_builder.build_idempotency_key(
            telegram_user_id=1,
            source_id="cb:123",
            kommo_lead_id=10,
            event_type="call",
            start_iso="2026-07-04T10:00:00+02:00",
        )
        key2 = calendar_event_builder.build_idempotency_key(
            telegram_user_id=1,
            source_id="cb:123",
            kommo_lead_id=10,
            event_type="call",
            start_iso="2026-07-04T10:00:00+02:00",
        )
        assert key1 == key2


class TestGoogleCalendarService:
    def test_diagnose_not_configured(self):
        with patch.object(google_calendar_service, "is_configured", return_value=False):
            info = google_calendar_service.diagnose_google_calendar()
        assert info["configured"] is False
        assert "не настроен" in (info.get("error") or "").lower()

    def test_create_event_success(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {
            "id": "evt123",
            "htmlLink": "https://calendar.google.com/event?eid=abc",
        }
        with patch.object(
            google_calendar_service, "configured_calendar_id", return_value="cal@test"
        ), patch.object(
            google_calendar_service, "_calendar_service", return_value=mock_service
        ):
            result = google_calendar_service.create_event(
                title="Созвон",
                description="test",
                start_iso="2026-07-04T10:00:00+02:00",
                end_iso="2026-07-04T10:30:00+02:00",
                reminder_minutes=30,
            )
        assert result["event_id"] == "evt123"
        assert "calendar.google.com" in result["event_url"]

    def test_diagnose_reader_role_shows_write_error(self):
        with patch.object(google_calendar_service, "is_configured", return_value=True), patch.object(
            google_calendar_service,
            "get_calendar_metadata",
            return_value={
                "summary": "B&BS Work",
                "access_role": "reader",
                "time_zone": "Europe/Warsaw",
            },
        ), patch.object(
            google_calendar_service,
            "service_account_email",
            return_value="bot@test.iam.gserviceaccount.com",
        ):
            info = google_calendar_service.diagnose_google_calendar()
        assert info["read_ok"] is True
        assert info["write_ok"] is False
        report = google_calendar_service.format_diagnostic_report(info)
        assert "только чтение" in report
        assert "bot@test.iam.gserviceaccount.com" in report

    def test_diagnose_write_probe_success(self):
        with patch.object(google_calendar_service, "is_configured", return_value=True), patch.object(
            google_calendar_service,
            "get_calendar_metadata",
            return_value={
                "summary": "B&BS Work",
                "access_role": "reader",
                "time_zone": "Europe/Warsaw",
            },
        ), patch.object(
            google_calendar_service,
            "create_event",
            return_value={"event_id": "evt1", "event_url": "https://example.com"},
        ), patch.object(google_calendar_service, "delete_event"), patch.object(
            google_calendar_service,
            "service_account_email",
            return_value="bot@test.iam.gserviceaccount.com",
        ):
            info = google_calendar_service.diagnose_google_calendar(
                include_write_probe=True
            )
        assert info["write_ok"] is True

    def test_write_permission_failure_message(self):
        class FakeHttpError(Exception):
            resp = MagicMock(status=403)

        with patch.object(
            google_calendar_service, "configured_calendar_id", return_value="cal@test"
        ), patch.object(
            google_calendar_service,
            "_calendar_service",
            side_effect=FakeHttpError("forbidden"),
        ):
            with pytest.raises(google_calendar_service.GoogleCalendarError) as exc:
                google_calendar_service.create_event(
                    title="x",
                    description="y",
                    start_iso="2026-07-04T10:00:00+02:00",
                    end_iso="2026-07-04T10:30:00+02:00",
                )
        assert "Недостаточно прав" in str(exc.value)


class TestCalendarFallback:
    def test_ics_fallback_on_failure(self):
        with patch.object(
            calendar_service,
            "_create_google_event",
            side_effect=calendar_service.CalendarIntegrationError("fail"),
        ), patch.object(calendar_service.settings, "calendar_provider", "google"):
            result = calendar_service.create_event_with_fallback(
                "Test",
                "Desc",
                "2026-07-04T10:00:00+02:00",
                30,
            )
        assert result["success"] is False
        assert result["ics_content"]
        assert "BEGIN:VCALENDAR" in result["ics_content"]


class TestTelegramAuthorization:
    def test_unauthorized_user(self):
        from app.api import telegram as telegram_api

        with patch.object(telegram_api.settings, "allowed_telegram_user_ids", "111"):
            assert telegram_api._is_allowed_user(111) is True
            assert telegram_api._is_allowed_user(999) is False
