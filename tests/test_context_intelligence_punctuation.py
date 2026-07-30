from app.agent.contracts import AgentPlan
from app.services import context_intelligence_runtime, kaizen_runtime


def _fallback(_text: str, _context: dict) -> AgentPlan | None:
    return None


def _plan(text: str, context: dict | None = None):
    context_intelligence_runtime.install_context_intelligence_runtime()
    return kaizen_runtime.smarter_deterministic_plan(_fallback, text, context or {})


def test_question_marks_do_not_break_short_commands():
    assert _plan("Кто ждёт нас?").intent == "waiting_us"
    assert _plan("Кого ждём?").intent == "waiting_client"
    assert _plan("Без движения?").intent == "stale_projects"


def test_question_mark_does_not_break_project_number():
    plan = _plan("Покажи 135?")
    assert plan.intent == "project_snapshot"
    assert plan.query == "135"
