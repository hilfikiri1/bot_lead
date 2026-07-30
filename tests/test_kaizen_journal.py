from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.contracts import AgentPlan
from app.models.kaizen_journal_entry import KaizenJournalEntry
from app.services import kaizen_journal_service, kaizen_runtime


ROOT = Path(__file__).resolve().parents[1]


def _fallback(_text: str, _context: dict) -> AgentPlan | None:
    return None


def test_evening_routes_to_reflection_not_daily_digest():
    plan = kaizen_runtime.smarter_deterministic_plan(_fallback, "/evening", {})
    assert plan is not None
    assert plan.intent == "daily_reflection"
    assert plan.intent != "daily_digest"


def test_natural_evening_phrase_uses_same_flow():
    plan = kaizen_runtime.smarter_deterministic_plan(
        _fallback, "Подведём итоги дня", {}
    )
    assert plan is not None
    assert plan.intent == "daily_reflection"


def test_week_and_natural_weekly_phrases_are_deterministic():
    assert kaizen_runtime.smarter_deterministic_plan(_fallback, "/week", {}).intent == "weekly_review"
    assert (
        kaizen_runtime.smarter_deterministic_plan(
            _fallback, "Какие проблемы повторялись на неделе?", {}
        ).intent
        == "weekly_review"
    )


def test_short_operational_commands_are_understood_without_llm():
    assert kaizen_runtime.smarter_deterministic_plan(_fallback, "что горит", {}).intent == "daily_digest"
    assert kaizen_runtime.smarter_deterministic_plan(_fallback, "план дня", {}).intent == "daily_plan"
    assert kaizen_runtime.smarter_deterministic_plan(_fallback, "без шага", {}).intent == "without_next_action"
    project = kaizen_runtime.smarter_deterministic_plan(_fallback, "проект 135", {})
    assert project.intent == "project_snapshot"
    assert project.query == "135"


def test_short_context_command_uses_active_project():
    plan = kaizen_runtime.smarter_deterministic_plan(
        _fallback,
        "что по нему",
        {"active_kommo_lead_id": 14471141},
    )
    assert plan.intent == "project_snapshot"
    assert plan.lead_id == 14471141


def test_append_diary_extracts_body():
    matched, body = kaizen_runtime._extract_append_text(
        "Дополни дневник: ещё потерял час на поиск прайса"
    )
    assert matched is True
    assert body == "ещё потерял час на поиск прайса"


def test_slash_command_is_not_intercepted_by_pending_reflection():
    assert kaizen_runtime._explicit_command_during_reflection("/today", {}) is True
    assert kaizen_runtime._explicit_command_during_reflection("/diag 135", {}) is True
    assert (
        kaizen_runtime._explicit_command_during_reflection(
            "Сегодня хорошо поговорил с клиентом, но потерял время на прайс", {}
        )
        is False
    )


@pytest.mark.asyncio
async def test_expired_pending_state_is_cleared():
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    session = SimpleNamespace(
        context={
            "pending_daily_reflection": {
                "local_date": kaizen_journal_service.local_date().isoformat(),
                "started_at": (expired - timedelta(hours=1)).isoformat(),
                "expires_at": expired.isoformat(),
                "source": "command",
            }
        }
    )
    db = SimpleNamespace(commit=AsyncMock())
    result = await kaizen_journal_service.active_pending_reflection(
        db, session=session
    )
    assert result is None
    assert "pending_daily_reflection" not in session.context
    db.commit.assert_awaited()


def test_pending_expiry_never_crosses_next_local_day():
    tz = kaizen_journal_service.manager_timezone()
    late = datetime(2026, 7, 30, 23, 30, tzinfo=tz)
    expires = kaizen_journal_service._pending_expiry(late).astimezone(tz)
    assert expires.date() == date(2026, 7, 31)
    assert expires.hour == 0
    assert expires.minute == 0


def test_daily_analysis_is_strictly_normalised():
    result = kaizen_journal_service.normalise_daily_analysis(
        {
            "good": ["Подготовил предложение", ""],
            "improvement_signals": [
                {
                    "area": "unknown-area",
                    "problem": "Прайсы разбросаны",
                    "evidence": "Искал актуальный файл в трёх чатах",
                    "possible_improvement": "Хранить актуальный прайс в проекте",
                    "confidence": 2,
                }
            ],
            "needs_followup": True,
            "followup_question": "Что именно потерялось?",
        }
    )
    assert result["good"] == ["Подготовил предложение"]
    assert result["improvement_signals"][0]["area"] == "other"
    assert result["improvement_signals"][0]["confidence"] == 1.0
    assert result["needs_followup"] is True


def test_weekly_analysis_marks_insufficient_data_and_clamps_candidates():
    candidates = [
        {
            "title": f"Improvement {index}",
            "problem": "Problem",
            "evidence": "Seen on multiple days",
            "proposed_action": "Change one process",
            "expected_effect": "Less repeated searching",
            "verification": "Check next Friday",
            "impact": "high",
            "effort": "low",
            "priority": index,
        }
        for index in range(1, 6)
    ]
    result = kaizen_journal_service.normalise_weekly_analysis(
        {"improvement_candidates": candidates, "insufficient_data": False},
        completed_days=2,
    )
    assert result["insufficient_data"] is True
    assert len(result["improvement_candidates"]) == 3


def test_weekly_problem_requires_two_days_of_evidence():
    result = kaizen_journal_service.normalise_weekly_analysis(
        {
            "recurring_problems": [
                {
                    "problem": "One day only",
                    "evidence": ["Monday"],
                    "days_count": 1,
                    "root_cause_hypothesis": "Unknown",
                    "confidence": 0.9,
                }
            ]
        },
        completed_days=4,
    )
    assert result["recurring_problems"] == []


def test_week_period_is_monday_to_sunday():
    start, end = kaizen_journal_service.week_period(date(2026, 7, 30))
    assert start == date(2026, 7, 27)
    assert end == date(2026, 8, 2)


def test_callback_data_stays_below_telegram_limit():
    markup = kaizen_journal_service.reflection_invitation_markup(date(2026, 7, 30))
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_model_and_migration_define_single_unique_period():
    assert KaizenJournalEntry.__tablename__ == "kaizen_journal_entries"
    names = {constraint.name for constraint in KaizenJournalEntry.__table__.constraints}
    assert "uq_kaizen_journal_user_type_period" in names
    migration = (ROOT / "migrations/versions/014_kaizen_journal_entries.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "014_kaizen_journal_entries"' in migration
    assert 'down_revision = "013_whatsapp_cloud_messages"' in migration
    assert "kaizen_journal_entries" in migration


def test_voice_pipeline_gives_agent_first_right_of_refusal():
    source = (ROOT / "app/tasks/voice_note_tasks.py").read_text(encoding="utf-8")
    agent_call = source.index("agent_service.handle_message")
    client_analysis = source.index("ai_analysis_service.analyse_transcript")
    assert agent_call < client_analysis
    assert "if agent_reply.handled:" in source
    assert 'source="voice"' in source


def test_runtime_intercepts_active_reflection_before_original_agent():
    source = (ROOT / "app/services/kaizen_runtime.py").read_text(encoding="utf-8")
    pending_check = source.index("active_pending_reflection")
    original_call = source.index("return await original_handle_message", pending_check)
    save_call = source.index("_save_reflection", pending_check)
    assert pending_check < save_call < original_call
    assert "source=source" in source


def test_raw_text_is_committed_before_openai_analysis():
    source = (ROOT / "app/services/kaizen_journal_service.py").read_text(
        encoding="utf-8"
    )
    local_commit = source.index("# Local-first commit before OpenAI")
    ai_call = source.index("parsed = await _structured_json", local_commit)
    assert local_commit < ai_call


def test_daily_entry_upsert_prevents_duplicate_day():
    source = (ROOT / "app/services/kaizen_journal_service.py").read_text(
        encoding="utf-8"
    )
    assert "get_or_create_entry" in source
    assert "except IntegrityError" in source
    assert "uq_kaizen_journal_user_type_period" in (
        ROOT / "app/models/kaizen_journal_entry.py"
    ).read_text(encoding="utf-8")


def test_skip_and_reminder_are_persistent_not_redis_only():
    source = (ROOT / "app/services/kaizen_journal_service.py").read_text(
        encoding="utf-8"
    )
    assert 'entry.status = "skipped"' in source
    assert "entry.remind_at = min(" in source
    assert "claim_due_reminders" in source
    assert "with_for_update(skip_locked=True)" in source


def test_scheduler_uses_database_claims_and_suppresses_duplicate_evening_digest():
    source = (ROOT / "app/services/agent_scheduled_digest_service.py").read_text(
        encoding="utf-8"
    )
    assert "claim_evening_invitation" in source
    assert "_claim_weekly_delivery" in source
    assert "automatic_delivery_pending" in source
    assert "claim_due_reminders" in source
    assert "elif (" in source
    assert "settings.agent_evening_digest_enabled" in source


def test_notion_write_is_staged_before_execution():
    source = (ROOT / "app/services/kaizen_runtime.py").read_text(encoding="utf-8")
    assert 'action_type="create_notion_improvements_batch"' in source
    assert "actions.stage_action" in source
    assert "actions.approval_markup(action.id)" in source
    assert 'if action.action_type == "create_notion_improvements_batch"' in source


def test_notion_batch_is_duplicate_safe_and_partial_failure_aware():
    source = (ROOT / "app/services/kaizen_runtime.py").read_text(encoding="utf-8")
    assert "entry.notion_page_ids" in source
    assert "item_results" in source
    assert '"partial_failed": failures > 0' in source
    assert "external_id = f\"kaizen:{entry.id}:{index}\"" in source


def test_notion_is_not_required_for_local_weekly_report(monkeypatch):
    monkeypatch.setattr(kaizen_journal_service.settings, "notion_api_token", "")
    monkeypatch.setattr(kaizen_journal_service.settings, "notion_tasks_data_source_id", "")
    assert kaizen_journal_service.notion_improvements_available() is False


def test_no_automatic_kommo_whatsapp_or_gmail_write_in_journal_service():
    source = (ROOT / "app/services/kaizen_journal_service.py").read_text(
        encoding="utf-8"
    )
    assert "update_kommo_lead(" not in source
    assert "send_whatsapp" not in source
    assert "create_draft(" not in source
