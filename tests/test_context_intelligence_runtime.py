from __future__ import annotations

from app.agent.contracts import AgentPlan
from app.services import context_intelligence_runtime, kaizen_runtime


def _fallback(_text: str, _context: dict) -> AgentPlan | None:
    return None


def _plan(text: str, context: dict):
    context_intelligence_runtime.install_context_intelligence_runtime()
    return kaizen_runtime.smarter_deterministic_plan(_fallback, text, context)


def test_expanded_daily_and_weekly_language():
    assert _plan("Разберём день", {}).intent == "daily_reflection"
    assert _plan("Как прошла неделя?", {}).intent == "weekly_review"


def test_more_short_read_commands():
    assert _plan("горящие", {}).intent == "daily_digest"
    assert _plan("кто ждёт нас", {}).intent == "waiting_us"
    assert _plan("кого ждём", {}).intent == "waiting_client"
    assert _plan("без движения", {}).intent == "stale_projects"


def test_bare_internal_number_opens_project():
    plan = _plan("135", {})
    assert plan.intent == "project_snapshot"
    assert plan.query == "135"


def test_short_note_requires_active_context():
    active = _plan("Запиши: клиент подтвердил образец", {"active_kommo_lead_id": 99})
    assert active.intent == "add_kommo_note"
    assert active.mode == "write"
    assert active.lead_id == 99
    assert active.note_text == "клиент подтвердил образец"

    assert _plan("Запиши: клиент подтвердил образец", {}) is None


def test_short_task_uses_active_project_and_still_requires_confirmation():
    plan = _plan("Перезвонить завтра в 10:00", {"active_kommo_lead_id": 99})
    assert plan.intent == "create_kommo_task"
    assert plan.mode == "write"
    assert plan.lead_id == 99
    assert plan.due_at == "Перезвонить завтра в 10:00"


def test_pronoun_followup_uses_active_project_and_language():
    plan = _plan("Ответить ему по-польски", {"active_kommo_lead_id": 99})
    assert plan.intent == "generate_draft"
    assert plan.mode == "draft"
    assert plan.lead_id == 99
    assert plan.draft_kind == "followup_message"
    assert plan.language == "pl"


def test_contextual_write_never_guesses_without_active_project():
    assert _plan("Перезвонить завтра", {}) is None
    assert _plan("Ответить ему по-польски", {}) is None
