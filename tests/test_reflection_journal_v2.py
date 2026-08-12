from datetime import date

from app.services.reflection_journal_v2_runtime import (
    PERSONAL_ENTRY_TYPE,
    _normalise,
    format_saved,
    invitation_markup,
    invitation_text,
)


def test_reflection_invitation_offers_company_and_personal_modes():
    markup = invitation_markup(date(2026, 8, 12))
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert any(button["text"] == "🏢 Рассказать о фирме" for button in buttons)
    assert any(button["text"] == "👤 Рассказать о себе" for button in buttons)
    assert any(
        button["callback_data"] == "agent:reflection:company:2026-08-12"
        for button in buttons
    )
    assert any(
        button["callback_data"] == "agent:reflection:personal:2026-08-12"
        for button in buttons
    )
    assert "полный смысл" in invitation_text().casefold()
    assert PERSONAL_ENTRY_TYPE == "daily_personal"


def test_resolved_bank_problem_is_not_presented_as_a_blocker():
    parsed = {
        "clean_text": "Подписал контракт с бухгалтером и разобрался с проблемами в банке.",
        "important_points": [
            {
                "category": "problem",
                "text": "Разобрался с проблемами в банке",
                "evidence": "разобрался с проблемами в банке",
            }
        ],
        "main_conclusion": None,
    }
    result = _normalise(parsed, parsed["clean_text"])
    assert result["important_points"][0]["category"] == "result"


def test_explicit_blocker_stays_problem():
    parsed = {
        "clean_text": "Банк заблокировал платёж и это помешало работе.",
        "important_points": [
            {
                "category": "problem",
                "text": "Банк заблокировал платёж и помешал работе",
                "evidence": "помешало работе",
            }
        ],
    }
    result = _normalise(parsed, parsed["clean_text"])
    assert result["important_points"][0]["category"] == "problem"


def test_daily_reply_is_not_artificially_limited_to_three_items():
    segment = {
        "scope": "company",
        "important_points": [
            {"category": "result", "text": f"Результат {index}", "evidence": ""}
            for index in range(1, 8)
        ],
        "main_conclusion": "Ставить давно запланированные дела выше в приоритете.",
        "notion_status": "ok",
    }
    text = format_saved(segment, True)
    for index in range(1, 8):
        assert f"Результат {index}" in text
    assert "Главный вывод" in text
    assert "добавлен в Notion" in text


def test_prompt_requires_full_fidelity_and_separates_observation_from_idea():
    from app.services.reflection_journal_v2_runtime import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.casefold()
    assert "preserve every substantive fact" in lowered
    assert "never arbitrarily choose only three" in lowered
    assert "separate an observation from an idea" in lowered
