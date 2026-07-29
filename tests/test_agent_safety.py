from app.agent.action_utils import approval_markup, action_key
from app.agent.contracts import AgentPlan


def test_confirmation_callback_fits_telegram_limit():
    markup = approval_markup(123456789)
    for row in markup["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode("utf-8")) <= 64


def test_action_key_changes_with_payload():
    first = action_key(telegram_user_id=1, action_type="add_kommo_note", payload={"lead_id": 1, "note_text": "a"})
    second = action_key(telegram_user_id=1, action_type="add_kommo_note", payload={"lead_id": 1, "note_text": "b"})
    assert first != second


def test_action_key_has_no_minute_bucket():
    source = open("app/agent/action_utils.py", encoding="utf-8").read()
    assert "%Y%m%d%H%M" not in source
    assert "strftime" not in source


def test_plan_rejects_invalid_confidence():
    try:
        AgentPlan(confidence=2)
    except Exception:
        pass
    else:
        raise AssertionError("confidence validation did not run")


def test_audit_redacts_tokens_and_passwords():
    from app.agent.security import sanitize_text

    text = sanitize_text("Authorization: Bearer abc.def password=hunter2 api_key=secret123")
    assert "abc.def" not in text
    assert "hunter2" not in text
    assert "secret123" not in text
    assert "***" in text


def test_reset_memory_deletes_recent_message_history():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app/agent/memory.py").read_text(
        encoding="utf-8"
    )
    assert "delete(AgentMessage)" in source


def test_lead_candidate_buttons_use_compact_callbacks():
    from app.agent.tools import candidates_markup

    markup = candidates_markup(
        [
            {"id": 123456, "name": "Очень длинное название сделки с клиентом и товаром для проверки"},
            {"id": 654321, "name": "Кормушки"},
        ]
    )

    assert markup is not None
    rows = markup["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "agent:lead:123456"
    assert rows[1][0]["callback_data"] == "agent:lead:654321"
    assert len(rows[0][0]["callback_data"].encode("utf-8")) <= 64


def test_lead_candidate_button_can_continue_drive_project_intent():
    from app.agent.tools import candidates_markup

    markup = candidates_markup(
        [{"id": 12496715, "name": "135 - Кормушки"}],
        next_intent="create_drive_project",
    )

    assert markup is not None
    callback_data = markup["inline_keyboard"][0][0]["callback_data"]
    assert callback_data == "agent:lead:12496715:create_drive_project"
    assert len(callback_data.encode("utf-8")) <= 64
