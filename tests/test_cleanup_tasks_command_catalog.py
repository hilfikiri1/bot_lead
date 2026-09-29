from app.services.telegram_command_catalog import COMMANDS


def test_cleanup_tasks_is_registered_in_telegram_catalog():
    commands = {item["command"] for item in COMMANDS}
    assert "cleanup_tasks" in commands
