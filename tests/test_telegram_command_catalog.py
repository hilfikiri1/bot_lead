from app.services.telegram_command_catalog import COMMANDS


def test_command_catalog_contains_kaizen_and_core_operations():
    names = [item["command"] for item in COMMANDS]
    assert "evening" in names
    assert "week" in names
    assert "plan" in names
    assert "digest" in names
    assert "diag" in names
    assert "comment_sync" in names
    assert len(names) == len(set(names))


def test_telegram_command_catalog_stays_within_platform_limit():
    assert len(COMMANDS) <= 100
    assert all(1 <= len(item["command"]) <= 32 for item in COMMANDS)
    assert all(1 <= len(item["description"]) <= 256 for item in COMMANDS)
