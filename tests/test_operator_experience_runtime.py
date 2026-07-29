from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import operator_experience_phone_patch as phone_patch
from app.services import operator_experience_runtime as runtime


def test_natural_conversation_update_extracts_product_query():
    text = (
        "Я поговорил с клиентом по кормушкам. Он хочет половину контейнера "
        "поилок и половину контейнера кормушек."
    )
    assert runtime._conversation_update_query(text) == "кормушкам"


def test_waiting_for_client_is_not_marked_urgent():
    state = SimpleNamespace(
        waiting_on="client",
        action_text="Дождаться ответа клиента WhatsApp",
        due_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    result = runtime._state_priority(
        {
            "score": 95,
            "priority": "Высокий",
            "reason": "нет следующей задачи",
            "next_step": "Определить следующее касание",
        },
        state,
        now=datetime.now(timezone.utc),
    )
    assert result["priority"] == "Низкий"
    assert result["reason"] == "ждём ответ клиента"
    assert result["actionable"] is False


def test_client_waiting_for_us_is_high_priority():
    state = SimpleNamespace(
        waiting_on="us",
        action_text="Отправить расчёт клиенту",
        due_at=None,
    )
    result = runtime._state_priority(
        {"score": 10, "priority": "Низкий", "reason": "недавно обновлялась"},
        state,
        now=datetime.now(timezone.utc),
    )
    assert result["priority"] == "Высокий"
    assert result["reason"] == "клиент ждёт наш ответ"
    assert result["actionable"] is True


def test_polish_local_and_international_phone_are_equivalent():
    assert phone_patch.phones_equivalent("+48 698 136 090", "698 136 090") is True
    assert phone_patch.phones_equivalent("+48 698 136 090", "698 136 091") is False


def test_lead_form_phone_matches_even_when_contact_phone_is_empty():
    phone_patch.install_operator_experience_phone_patch()
    row = SimpleNamespace(phone="+48 698 136 090", email=None)
    details = {
        "contacts": [{"id": 10, "name": "Przemek", "phones": [], "emails": []}],
        "custom_fields": [
            {
                "name": "Proszę podać swój numer kontaktowy",
                "code": "",
                "value": "698 136 090",
            }
        ],
    }
    assert runtime._lead_exactly_matches_row(details, row) is True


def test_email_in_lead_form_is_an_exact_fallback_match():
    row = SimpleNamespace(phone=None, email="client@example.com")
    details = {
        "contacts": [],
        "custom_fields": [
            {"name": "Email", "code": "EMAIL", "value": "CLIENT@example.com"}
        ],
    }
    assert runtime._lead_exactly_matches_row(details, row) is True


def test_discrepancy_text_is_manager_friendly():
    assert runtime._friendly_discrepancy("№118: нет ProjectLink") == (
        "№118: нет связки проекта с Notion/Drive"
    )
