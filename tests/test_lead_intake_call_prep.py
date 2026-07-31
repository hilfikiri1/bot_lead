"""Richer call preparation + contact card after Prepare call."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api import lead_intake_telegram
from app.services.lead_intake import repository, telegram_ui
from app.services.lead_intake.matching import LeadSnapshot
from app.services.lead_intake.schema import LeadQualification
from tests.lead_intake_helpers import temp_db_session

CALL_QUALIFICATION = {
    "product_name_ru": "Сельхозтовары",
    "potential": "high",
    "readiness": "low",
    "priority": "C",
    "priority_label_ru": "квалификация",
    "recommended_action": "phone_call",
    "recommended_action_reason_ru": "Нужен звонок, чтобы снять неопределённость.",
    "lead_analysis_ru": "Запрос слишком общий.",
    "main_risks_ru": ["Слишком широкая категория"],
    "missing_information_ru": ["Конкретная группа товаров"],
    "next_steps_ru": ["Позвонить и уточнить направление"],
    "client_message": {
        "language": "pl",
        "channel": "phone_call",
        "text": "Dzień dobry Panie Marcinie",
    },
    "call_script": {
        "objective": "За 5–10 минут понять направление сотрудничества",
        "company_context_ru": "По названию похоже на аграрного дистрибьютора.",
        "personal_analysis_ru": (
            "Это может быть сильный B2B-лид, но формулировка produkty rolne слишком общая."
        ),
        "priority_note_ru": "C — квалификация; потенциал A2 при регулярных закупках.",
        "main_question_pl": (
            "Czy interesuje Pana zakup z Chin, czy sprzedaż polskich produktów?"
        ),
        "main_question_reason_ru": "Нужно сразу понять направление запроса.",
        "conversation_script_pl": (
            "Dzień dobry Panie Marcinie,\nz tej strony [Ваше имя] z Buy & Bring Solutions.\n"
            "Czy interesuje Pana zakup z Chin, czy sprzedaż polskich produktów?"
        ),
        "opening_phrase": "Dzień dobry Panie Marcinie",
        "questions": [
            "Jakiej grupy produktów Pan poszukuje?",
            "Jakie są orientacyjne ilości?",
        ],
        "clarify_points_ru": [
            "Направление: закупка из Китая или продажа в Китай",
            "Конкретная категория и объёмы",
        ],
        "cheat_sheet_ru": [
            "Старт: получил заявку, хочу понять направление",
            "Первый вопрос: закупка из Китая или продажа польской продукции?",
        ],
        "possible_objections": ["Nie jestem jeszcze gotowy"],
        "recommended_answers": ["Rozumiem, proszę dać znać, kiedy będzie Pan gotowy."],
        "must_record_after_call": ["Направление запроса", "Категория товара"],
        "closing_phrase": "Dziękuję za rozmowę, czekam na listę produktów.",
    },
    "kommo_note_ru": "note",
    "task": {
        "type": "call",
        "title_ru": "Позвонить и уточнить направление",
        "due_rule": "today",
        "due_at": None,
    },
    "second_follow_up": {"enabled": False, "days_after_first": 0, "text": None},
}


def test_render_call_script_includes_analysis_and_main_question():
    qualification = LeadQualification.model_validate(CALL_QUALIFICATION)
    job = type("Job", (), {"id": 1, "assigned_number": "190", "kommo_lead_id": 1})()
    snapshot = LeadSnapshot(
        facebook_lead_id="1",
        phone="500600700",
        email=None,
        name="Marcin",
        product="produkty rolne",
        region="Polska",
        created_at=None,
    )
    messages, keyboard = telegram_ui.render_call_script(
        qualification, job, snapshot=snapshot
    )
    joined = "\n".join(messages)
    assert "ЛИЧНЫЙ АНАЛИЗ" in joined
    assert "ГЛАВНЫЙ ВОПРОС" in joined
    assert "СЦЕНАРИЙ РАЗГОВОРА" in joined
    assert "ШПАРГАЛКА" in joined
    assert "produkty rolne" in joined or "сильный B2B" in joined
    assert any(
        btn.get("url", "").startswith("tel:")
        for row in keyboard["inline_keyboard"]
        for btn in row
    )


@pytest.mark.asyncio
async def test_prepare_call_sends_script_and_vcard():
    async with temp_db_session() as db:
        job = await repository.create_job(
            db,
            kommo_lead_id=555190,
            original_title="Facebook #190",
            facebook_lead_id="190",
            facebook_technical_tag=None,
            source="facebook_lead_ads",
            raw_snapshot={
                "name": "Marcin",
                "phone": "500600700",
                "email": "m@example.com",
                "product": "produkty rolne",
            },
            dry_run=False,
            processing_version=2,
        )
        job = await repository.save(
            db,
            job,
            status="waiting_approval",
            assigned_number="190",
            ai_payload_json=CALL_QUALIFICATION,
        )

        send_message = AsyncMock()
        send_document = AsyncMock()
        with (
            patch(
                "app.api.lead_intake_telegram.telegram_service.send_message",
                new=send_message,
            ),
            patch(
                "app.api.lead_intake_telegram.telegram_service.send_document",
                new=send_document,
            ),
        ):
            handled = await lead_intake_telegram.handle_callback(
                callback_data=f"lp:call:{job.id}",
                chat_id=42,
                user_id=7,
                db=db,
            )

        assert handled is True
        assert send_message.await_count >= 1
        body = "\n".join(call.args[1] for call in send_message.await_args_list)
        assert "ЛИЧНЫЙ АНАЛИЗ" in body
        assert "ГЛАВНЫЙ ВОПРОС" in body
        send_document.assert_awaited_once()
        assert send_document.await_args.kwargs["mime_type"] == "text/vcard"
        assert "Контакт для iPhone" in send_document.await_args.kwargs["caption"]
