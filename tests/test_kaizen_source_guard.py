from app.services.kaizen_source_guard_runtime import (
    redact_journal_text,
    storage_source,
)


def test_documented_journal_sources_are_preserved():
    assert storage_source("text") == "text"
    assert storage_source("voice") == "voice"
    assert storage_source("scheduled") == "scheduled"
    assert storage_source("system") == "system"


def test_pending_command_source_is_stored_as_system_entry_source():
    assert storage_source("command") == "system"
    assert storage_source("") == "system"


def test_known_tokens_and_assignments_are_redacted():
    raw = (
        "Сегодня настроил интеграцию. "
        "api_key=sk-1234567890abcdefghijklmnop и "
        "refresh_token=ya29.abcdefghijklmnopqrstuvwxyz123456"
    )
    clean = redact_journal_text(raw)
    assert "sk-123" not in clean
    assert "ya29." not in clean
    assert "Сегодня настроил интеграцию" in clean
    assert "СКРЫТО" in clean


def test_private_key_json_and_pem_are_redacted():
    raw = (
        '{"client_email":"bot@example.com","private_key":"-----BEGIN PRIVATE KEY-----\\nABCDEF\\n-----END PRIVATE KEY-----"}\n'
        "-----BEGIN PRIVATE KEY-----\nREALKEY\n-----END PRIVATE KEY-----"
    )
    clean = redact_journal_text(raw)
    assert "REALKEY" not in clean
    assert "ABCDEF" not in clean
    assert clean.count("СКРЫТО") >= 1


def test_business_numbers_and_project_names_are_preserved():
    raw = "Проект 135, бюджет 20 000 USD, клиент Artur Cukrowski."
    assert redact_journal_text(raw) == raw
