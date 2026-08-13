from datetime import datetime, timezone

from app.services import business_day_followup_runtime, followup_service


def test_friday_tomorrow_is_monday(monkeypatch):
    monkeypatch.setattr(followup_service.settings, "manager_timezone", "Europe/Warsaw")
    now = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)
    due = business_day_followup_runtime.business_preset_due_at("tomorrow", now=now)
    assert due.astimezone(followup_service._manager_tz()).strftime("%d.%m.%Y %H:%M") == "17.08.2026 10:00"


def test_three_days_counts_business_days(monkeypatch):
    monkeypatch.setattr(followup_service.settings, "manager_timezone", "Europe/Warsaw")
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
    due = business_day_followup_runtime.business_preset_due_at("3d", now=now)
    assert due.astimezone(followup_service._manager_tz()).strftime("%d.%m.%Y %H:%M") == "18.08.2026 10:00"


def test_weekend_deadline_rolls_to_monday(monkeypatch):
    monkeypatch.setattr(followup_service.settings, "manager_timezone", "Europe/Warsaw")
    saturday = datetime(2026, 8, 15, 13, 30, tzinfo=timezone.utc)
    due = business_day_followup_runtime.roll_forward_to_business_day(saturday)
    assert due.astimezone(followup_service._manager_tz()).strftime("%d.%m.%Y %H:%M") == "17.08.2026 15:30"


def test_weekend_detection(monkeypatch):
    monkeypatch.setattr(followup_service.settings, "manager_timezone", "Europe/Warsaw")
    assert business_day_followup_runtime.is_weekend(datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc))
    assert not business_day_followup_runtime.is_weekend(datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc))
