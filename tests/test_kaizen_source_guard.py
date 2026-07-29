from app.services.kaizen_source_guard_runtime import storage_source


def test_documented_journal_sources_are_preserved():
    assert storage_source("text") == "text"
    assert storage_source("voice") == "voice"
    assert storage_source("scheduled") == "scheduled"
    assert storage_source("system") == "system"


def test_pending_command_source_is_stored_as_system_entry_source():
    assert storage_source("command") == "system"
    assert storage_source("") == "system"
