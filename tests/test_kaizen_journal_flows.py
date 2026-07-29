from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import kaizen_journal_service


def _daily_entry(*, raw_text: str | None = None, status: str = "open"):
    return SimpleNamespace(
        id=7,
        telegram_user_id=101,
        entry_type="daily",
        period_start=date(2026, 7, 30),
        period_end=date(2026, 7, 30),
        status=status,
        source="text",
        raw_text=raw_text,
        analysis={},
        notion_page_ids=[],
        remind_at=None,
    )


@pytest.mark.asyncio
async def test_daily_text_is_saved_and_structured(monkeypatch):
    entry = _daily_entry()
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    session = SimpleNamespace(context={"pending_daily_reflection": {"local_date": "2026-07-30"}})

    async def get_or_create(*args, **kwargs):
        return entry

    async def structured(*args, **kwargs):
        # The first DB commit must already have happened before AI is invoked.
        assert db.commit.await_count >= 2  # clear pending + local-first entry commit
        assert "Подготовил предложение" in entry.raw_text
        return {
            "good": ["Подготовил предложение"],
            "difficulties": ["Долго искал прайс"],
            "lessons": ["Нужна единая папка"],
            "tomorrow_focus": ["Отправить цену клиенту"],
        }

    monkeypatch.setattr(kaizen_journal_service, "get_or_create_entry", get_or_create)
    monkeypatch.setattr(kaizen_journal_service, "_structured_json", structured)
    monkeypatch.setattr(kaizen_journal_service, "local_date", lambda now=None: date(2026, 7, 30))

    saved, analysis_ok = await kaizen_journal_service.save_daily_reflection(
        db,
        telegram_user_id=101,
        session=session,
        text="Подготовил предложение, но долго искал прайс. Завтра отправить цену клиенту.",
        source="text",
        now=datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc),
    )

    assert saved is entry
    assert analysis_ok is True
    assert entry.status == "completed"
    assert entry.source == "text"
    assert entry.analysis["good"] == ["Подготовил предложение"]
    assert "pending_daily_reflection" not in session.context


@pytest.mark.asyncio
async def test_voice_transcript_survives_openai_failure(monkeypatch):
    entry = _daily_entry()
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    session = SimpleNamespace(context={"pending_daily_reflection": {"local_date": "2026-07-30"}})

    async def get_or_create(*args, **kwargs):
        return entry

    async def broken_ai(*args, **kwargs):
        assert "голосовой рассказ" in entry.raw_text
        raise TimeoutError("OpenAI unavailable")

    monkeypatch.setattr(kaizen_journal_service, "get_or_create_entry", get_or_create)
    monkeypatch.setattr(kaizen_journal_service, "_structured_json", broken_ai)
    monkeypatch.setattr(kaizen_journal_service, "local_date", lambda now=None: date(2026, 7, 30))

    saved, analysis_ok = await kaizen_journal_service.save_daily_reflection(
        db,
        telegram_user_id=101,
        session=session,
        text="Это голосовой рассказ: хорошо поговорил с клиентом, но потерял время на документы.",
        source="voice",
    )

    assert analysis_ok is False
    assert saved.raw_text.startswith("Это голосовой рассказ")
    assert saved.source == "voice"
    assert saved.status == "completed"
    assert saved.analysis["analysis_unavailable"] is True
    assert db.commit.await_count >= 3


@pytest.mark.asyncio
async def test_second_answer_appends_to_same_daily_entry(monkeypatch):
    entry = _daily_entry(raw_text="Сначала подготовил предложение.", status="completed")
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    session = SimpleNamespace(context={})

    async def get_or_create(*args, **kwargs):
        return entry

    async def structured(*args, **kwargs):
        return {"ideas": ["Сохранять финальные прайсы в проекте"]}

    monkeypatch.setattr(kaizen_journal_service, "get_or_create_entry", get_or_create)
    monkeypatch.setattr(kaizen_journal_service, "_structured_json", structured)
    monkeypatch.setattr(kaizen_journal_service, "local_date", lambda now=None: date(2026, 7, 30))

    saved, ok = await kaizen_journal_service.save_daily_reflection(
        db,
        telegram_user_id=101,
        session=session,
        text="Ещё понял, что финальные прайсы нужно хранить в проекте.",
        source="text",
        append=True,
    )

    assert ok is True
    assert saved is entry
    assert saved.raw_text.count("Дополнение:") == 1
    assert "Сначала подготовил" in saved.raw_text
    assert "Ещё понял" in saved.raw_text


@pytest.mark.asyncio
async def test_weekly_review_uses_only_selected_week_and_clamps_items(monkeypatch):
    entries = [
        SimpleNamespace(
            period_start=date(2026, 7, 27 + index),
            raw_text=f"День {index + 1}: искал актуальный прайс",
            analysis={"good": [f"Результат {index + 1}"]},
        )
        for index in range(3)
    ]
    weekly = SimpleNamespace(
        id=20,
        telegram_user_id=101,
        entry_type="weekly",
        period_start=date(2026, 7, 27),
        period_end=date(2026, 8, 2),
        status="open",
        source="system",
        raw_text=None,
        analysis={},
        notion_page_ids=[],
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock())
    captured = {}

    async def daily_rows(db_arg, *, telegram_user_id, start, end):
        assert start == date(2026, 7, 27)
        assert end == date(2026, 8, 2)
        return entries

    async def get_or_create(*args, **kwargs):
        return weekly

    async def operational(*args, **kwargs):
        return {"overdue": 2, "without_next": 1}, None

    async def structured(_system, user):
        captured["prompt"] = user
        return {
            "summary": "Прайсы искались повторно.",
            "recurring_problems": [
                {
                    "problem": "Поиск актуального прайса",
                    "evidence": ["27.07", "28.07", "29.07"],
                    "days_count": 3,
                    "root_cause_hypothesis": "Нет единого места хранения",
                    "confidence": 0.8,
                }
            ],
            "improvement_candidates": [
                {
                    "title": f"Кандидат {index}",
                    "problem": "Прайсы разбросаны",
                    "evidence": "Упоминалось в трёх днях",
                    "proposed_action": "После получения сохранять финальный прайс в папку проекта",
                    "expected_effect": "Меньше повторного поиска",
                    "verification": "Через неделю проверить повторные поиски",
                    "impact": "high",
                    "effort": "low",
                    "priority": index,
                }
                for index in range(1, 6)
            ],
        }

    monkeypatch.setattr(kaizen_journal_service, "daily_entries_for_week", daily_rows)
    monkeypatch.setattr(kaizen_journal_service, "get_or_create_entry", get_or_create)
    monkeypatch.setattr(kaizen_journal_service, "_operational_counts", operational)
    monkeypatch.setattr(kaizen_journal_service, "_structured_json", structured)
    monkeypatch.setattr(kaizen_journal_service, "local_date", lambda now=None: date(2026, 7, 30))

    saved, ok = await kaizen_journal_service.build_weekly_review(
        db,
        telegram_user_id=101,
        week_start=date(2026, 7, 27),
        force_rebuild=True,
    )

    assert ok is True
    assert saved is weekly
    assert weekly.status == "completed"
    assert len(weekly.analysis["improvement_candidates"]) == 3
    assert weekly.analysis["insufficient_data"] is False
    assert "День 1" in captured["prompt"]
    assert "2026-07-27" in captured["prompt"]
    assert weekly.analysis["operational_counts"]["overdue"] == 2


@pytest.mark.asyncio
async def test_weekly_report_still_completes_when_ai_is_down(monkeypatch):
    entries = [
        SimpleNamespace(
            period_start=date(2026, 7, 27),
            raw_text="Подготовил предложение",
            analysis={"good": ["Подготовил предложение"]},
        )
    ]
    weekly = SimpleNamespace(
        id=21,
        telegram_user_id=101,
        entry_type="weekly",
        period_start=date(2026, 7, 27),
        period_end=date(2026, 8, 2),
        status="open",
        source="system",
        raw_text=None,
        analysis={},
        notion_page_ids=[],
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), rollback=AsyncMock())

    async def daily_rows(*args, **kwargs):
        return entries

    async def get_or_create(*args, **kwargs):
        return weekly

    async def operational(*args, **kwargs):
        return {}, "KommoUnavailable"

    async def broken(*args, **kwargs):
        raise RuntimeError("AI down")

    monkeypatch.setattr(kaizen_journal_service, "daily_entries_for_week", daily_rows)
    monkeypatch.setattr(kaizen_journal_service, "get_or_create_entry", get_or_create)
    monkeypatch.setattr(kaizen_journal_service, "_operational_counts", operational)
    monkeypatch.setattr(kaizen_journal_service, "_structured_json", broken)

    saved, ok = await kaizen_journal_service.build_weekly_review(
        db,
        telegram_user_id=101,
        week_start=date(2026, 7, 27),
        force_rebuild=True,
    )

    assert ok is False
    assert saved.status == "completed"
    assert saved.analysis["analysis_unavailable"] is True
    assert saved.analysis["crm_snapshot_unavailable"] == "KommoUnavailable"
    assert saved.analysis["insufficient_data"] is True
