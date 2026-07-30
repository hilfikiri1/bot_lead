from __future__ import annotations

from app.services.next_action_service import (
    InboxResult,
    NextActionView,
    _fallback_recommendation,
    format_plan,
)


def _view() -> NextActionView:
    return NextActionView(
        kommo_lead_id=166,
        internal_number="166",
        name="166 - Чай",
        status="overdue",
        waiting_on=None,
        action_text=None,
        due_at=None,
        stale_reason="просрочена задача",
        recommended_action="Закрыть или перенести просроченную задачу",
        category="overdue",
    )


def test_real_kommo_task_replaces_generic_overdue_text():
    view = _view()
    _fallback_recommendation(
        view,
        tasks=[{"text": "Уточнить у клиента объём и требования к упаковке"}],
        notes=[],
    )
    assert view.recommended_action == "Уточнить у клиента объём и требования к упаковке"
    assert "ближайшая незавершённая задача" in str(view.action_reason)


def test_plan_explains_what_why_and_message():
    view = _view()
    view.recommended_action = "Написать клиенту и уточнить планируемый объём"
    view.action_reason = "Без объёма нельзя запросить фабрику."
    view.suggested_message = "Добрый день! Какой объём чая вы планируете заказать?"
    text = format_plan(InboxResult(overdue=[view]))
    assert "Что сделать:" in text
    assert "Почему:" in text
    assert "Что написать:" in text
    assert "Закрыть или перенести" not in text
