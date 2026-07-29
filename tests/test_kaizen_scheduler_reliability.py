from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import agent_scheduled_digest_service as scheduler


@pytest.fixture(autouse=True)
def _simple_json_flag(monkeypatch):
    monkeypatch.setattr(scheduler, "flag_modified", lambda *_args, **_kwargs: None)


def _entry(*, status="open", analysis=None, source="scheduled"):
    return SimpleNamespace(
        id=20,
        telegram_user_id=101,
        entry_type="weekly",
        period_start=date(2026, 7, 27),
        period_end=date(2026, 8, 2),
        status=status,
        source=source,
        analysis=analysis or {},
        remind_at=None,
    )


@pytest.mark.asyncio
async def test_failed_reminder_is_rearmed_twice_then_abandoned():
    db = SimpleNamespace(commit=AsyncMock())
    entry = _entry()

    await scheduler._rearm_failed_reminder(db, entry)
    assert entry.analysis["scheduler"]["reminder_delivery_failures"] == 1
    assert entry.remind_at is not None

    await scheduler._rearm_failed_reminder(db, entry)
    assert entry.analysis["scheduler"]["reminder_delivery_failures"] == 2
    assert entry.remind_at is not None

    await scheduler._rearm_failed_reminder(db, entry)
    assert entry.analysis["scheduler"]["reminder_delivery_failures"] == 3
    assert entry.remind_at is None
    assert entry.analysis["scheduler"]["reminder_abandoned_at"]


@pytest.mark.asyncio
async def test_weekly_delivery_markers_prevent_duplicate_after_success():
    db = SimpleNamespace(commit=AsyncMock())
    entry = _entry(status="completed")

    await scheduler._set_weekly_delivery_pending(db, entry)
    assert entry.analysis["scheduler"]["automatic_delivery_pending"] is True

    await scheduler._mark_weekly_delivery_sent(db, entry)
    state = entry.analysis["scheduler"]
    assert state["automatic_delivery_pending"] is False
    assert state["weekly_sent_at"]
    assert "weekly_claimed_at" not in state


@pytest.mark.asyncio
async def test_failed_weekly_delivery_releases_claim_but_keeps_pending():
    db = SimpleNamespace(commit=AsyncMock())
    entry = _entry(
        status="completed",
        analysis={
            "scheduler": {
                "automatic_delivery_pending": True,
                "weekly_claimed_at": "2026-07-30T12:00:00+00:00",
            }
        },
    )

    await scheduler._release_weekly_delivery_claim(db, entry)
    state = entry.analysis["scheduler"]
    assert state["automatic_delivery_pending"] is True
    assert "weekly_claimed_at" not in state


@pytest.mark.asyncio
async def test_manual_completed_week_is_not_claimed_for_automatic_delivery(monkeypatch):
    entry = _entry(status="completed", analysis={}, source="system")
    db = SimpleNamespace(commit=AsyncMock())

    monkeypatch.setattr(
        scheduler.kaizen_journal_service,
        "week_period",
        lambda: (entry.period_start, entry.period_end),
    )
    monkeypatch.setattr(
        scheduler.kaizen_journal_service,
        "get_entry",
        AsyncMock(return_value=entry),
    )

    claimed = await scheduler._claim_weekly_delivery(db, 101)
    assert claimed is None
    db.commit.assert_not_awaited()


def test_failed_delivery_cleanup_is_present_before_retry():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "app/services/agent_scheduled_digest_service.py"
    ).read_text(encoding="utf-8")
    reminder_failure = source.index("Kaizen reminder failed")
    cleanup = source.index("clear_pending_reflection", reminder_failure)
    rearm = source.index("_rearm_failed_reminder", cleanup)
    assert reminder_failure < cleanup < rearm
