from app.services.operational_command_router import route


def test_routes_daily_digest_phrase():
    command = route("что делать сегодня")
    assert command is not None
    assert command.intent == "digest"


def test_routes_offer_draft_with_lead_id():
    command = route("сделай КП по 123456")
    assert command is not None
    assert command.intent == "draft"
    assert command.args == {"kind": "commercial_offer", "lead_id": 123456}


def test_unknown_text_is_not_treated_as_command():
    assert route("Добрый день, когда можно созвониться?") is None
