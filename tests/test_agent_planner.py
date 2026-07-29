from app.agent.planner import deterministic_plan


def test_digest_is_read_only():
    plan = deterministic_plan("что делать сегодня", {})
    assert plan is not None
    assert plan.intent == "daily_digest"
    assert plan.mode == "read"


def test_offer_draft_uses_active_lead():
    plan = deterministic_plan(
        "сделай КП по этой сделке на польском",
        {"active_kommo_lead_id": 123456},
    )
    assert plan is not None
    assert plan.intent == "generate_draft"
    assert plan.mode == "draft"
    assert plan.lead_id == 123456
    assert plan.draft_kind == "commercial_offer"
    assert plan.language == "pl"


def test_external_note_is_write_and_needs_confirmation():
    plan = deterministic_plan(
        "добавь примечание в #123456: клиент ждёт цену",
        {},
    )
    assert plan is not None
    assert plan.intent == "add_kommo_note"
    assert plan.mode == "write"
    assert plan.lead_id == 123456
    assert plan.note_text == "клиент ждёт цену"


def test_small_lead_number_is_search_query_not_kommo_id():
    plan = deterministic_plan("покажи сделку 135 кормушки", {})
    assert plan is not None
    assert plan.lead_id is None
    assert plan.query == "135"


def test_normal_client_message_not_forced_into_write():
    assert deterministic_plan("Добрый день, ожидаю ваш ответ по цене", {}) is None


def test_followup_can_save_last_draft_to_notion():
    plan = deterministic_plan("сохрани его в Notion", {})
    assert plan is not None
    assert plan.intent == "save_draft_to_notion"
    assert plan.mode == "write"


def test_followup_can_create_gmail_draft():
    plan = deterministic_plan("создай письмо в Gmail", {})
    assert plan is not None
    assert plan.intent == "create_gmail_draft"
    assert plan.mode == "write"
