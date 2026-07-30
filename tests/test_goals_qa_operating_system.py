from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.services import goals_qa_service
from app.services.goals_qa_runtime import (
    _explicit_save,
    _goal_capture,
    _normal,
    _qa_body,
    _qa_type_from_command,
    _when_reply,
)
from app.services.qa_projection_runtime import _payload


ROOT = Path(__file__).resolve().parents[1]


def test_migration_015_is_single_successor_of_kaizen() -> None:
    source = (ROOT / "migrations/versions/015_goals_and_qa_operating_system.py").read_text()
    assert 'revision = "015_goals_and_qa"' in source
    assert 'down_revision = "014_kaizen_journal_entries"' in source
    assert '"business_goals"' in source
    assert '"qa_issues"' in source
    assert '"qa_attachments"' in source


def test_models_are_registered_and_runtime_installed_last() -> None:
    models = (ROOT / "app/models/__init__.py").read_text()
    main = (ROOT / "app/main.py").read_text()
    assert "BusinessGoal" in models
    assert "QAIssue" in models
    assert "QAAttachment" in models
    assert main.index("install_goals_qa_runtime()") < main.index(
        "install_qa_projection_runtime()"
    )


def test_secret_redaction_covers_credentials_and_private_keys() -> None:
    raw = (
        "DATABASE_URL=postgresql://user:pass@db/x\n"
        "REDIS_URL=redis://:secret@redis/0\n"
        "Authorization: Bearer abcdef\n"
        "-----BEGIN PRIVATE KEY-----\nvery-secret\n-----END PRIVATE KEY-----"
    )
    cleaned = goals_qa_service.redact_sensitive(raw)
    assert "user:pass" not in cleaned
    assert "secret@redis" not in cleaned
    assert "abcdef" not in cleaned
    assert "very-secret" not in cleaned
    assert "[REDACTED]" in cleaned


def test_issue_classification_and_module_are_deterministic() -> None:
    text = "Google Drive не загружает файл и показывает ошибку"
    assert goals_qa_service.classify_issue(text) == "Integration issue"
    assert goals_qa_service.infer_module(text) == "Google Drive"
    assert goals_qa_service.infer_priority(text) == "High"


def test_dedupe_key_is_stable_but_module_sensitive() -> None:
    first = goals_qa_service.issue_dedupe_key(
        issue_type="Bug",
        module="Google Drive",
        title="Файл не загрузился",
        description="Файл не загрузился после подтверждения",
    )
    second = goals_qa_service.issue_dedupe_key(
        issue_type="Bug",
        module="Google Drive",
        title="  файл НЕ загрузился ",
        description="Файл не загрузился после подтверждения",
    )
    other = goals_qa_service.issue_dedupe_key(
        issue_type="Bug",
        module="Notion",
        title="Файл не загрузился",
        description="Файл не загрузился после подтверждения",
    )
    assert first == second
    assert first != other


def test_failed_attachment_is_not_counted_as_uploaded() -> None:
    issue = SimpleNamespace(
        id=1,
        issue_code="BUG-0001",
        title="Upload failed",
        issue_type="Bug",
        status="New",
        priority="High",
        module="Google Drive",
        active_project_number="135",
        trace_id=None,
        description="Drive failed",
        notion_url=None,
        attachments=[
            SimpleNamespace(upload_status="failed"),
            SimpleNamespace(upload_status="pending"),
            SimpleNamespace(upload_status="uploaded"),
        ],
    )
    rendered = goals_qa_service.format_issue(issue)
    assert "загружено 1" in rendered
    assert "ожидает 1" in rendered
    assert "ошибок 1" in rendered


def test_notion_files_payload_contains_only_real_external_links() -> None:
    payload = _payload(
        {"type": "files"},
        [
            {"name": "screen.png", "url": "https://drive.google.com/file/1"},
            {"name": "missing.png", "url": None},
        ],
    )
    assert payload == {
        "files": [
            {
                "name": "screen.png",
                "type": "external",
                "external": {"url": "https://drive.google.com/file/1"},
            }
        ]
    }


def test_qa_short_commands_and_explicit_save() -> None:
    assert _qa_type_from_command("/bug") == "Bug"
    assert _qa_type_from_command("Запиши идею: добавить кнопку") == "Improvement"
    assert _qa_type_from_command("У меня есть опасение: клиенты смешаются") == "Concern"
    assert _qa_body("/bug файл не появился") == "файл не появился"
    assert _explicit_save("Добавь баг: файл не появился") is True
    assert _explicit_save("/bug") is False


def test_short_commands_ignore_terminal_punctuation() -> None:
    assert _normal("Что сейчас самое важное?") == "что сейчас самое важное"
    assert _normal("/bugs!") == "/bugs"


def test_goal_capture_supports_command_and_natural_language() -> None:
    assert _goal_capture("/goal Закрыть три сделки") == "Закрыть три сделки"
    assert (
        _goal_capture("Добавь цель на месяц: получить пять новых клиентов")
        == "получить пять новых клиентов"
    )


def test_when_recommendation_respects_china_working_window() -> None:
    reply = _when_reply("Когда лучше написать фабрике в Китае?")
    assert "05:00–07:00" in reply.text
    assert "Событие в календаре не создавалось" in reply.text


def test_month_progress_never_invents_percentage() -> None:
    goal = SimpleNamespace(
        title="Запустить новый канал",
        status="active",
        progress_percent=None,
        current_value=None,
        target_value=None,
        next_step="Согласовать план",
        obstacles=None,
    )
    rendered = goals_qa_service.format_month_goals([goal], progress_view=True)
    assert "нет измеримых данных" in rendered
    assert "%" not in rendered


def test_retest_results_use_actual_russian_notion_values() -> None:
    assert goals_qa_service.RETEST_RESULTS == {
        "исправлено": "Исправлено",
        "частично": "Частично исправлено",
        "не исправлено": "Не исправлено",
        "новая проблема": "Появилась новая проблема",
    }


def test_public_api_version_is_not_changed_by_feature() -> None:
    main = (ROOT / "app/main.py").read_text()
    assert 'APP_VERSION = "5.0.0"' in main


def test_goal_period_defaults_are_valid() -> None:
    start, end = goals_qa_service.month_bounds(date(2026, 7, 15))
    assert start == date(2026, 7, 1)
    assert end == date(2026, 7, 31)
