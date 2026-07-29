from __future__ import annotations

import json

import pytest

from app.services import diagnostic_runtime
from app.services.system_diagnostics import (
    _overall_status,
    _safe_value,
    format_diagnostic_summary,
    render_diagnostic_json,
    render_diagnostic_markdown,
)


def _sample_report() -> dict:
    return {
        "schema_version": 1,
        "trace_id": "diag-20260729-230000-abc123",
        "started_at": "2026-07-29T23:00:00+00:00",
        "finished_at": "2026-07-29T23:00:01+00:00",
        "duration_ms": 1000,
        "overall_status": "PASS WITH WARNINGS",
        "project_query": "107",
        "checks": [
            {
                "name": "database",
                "status": "PASS",
                "detail": "PostgreSQL отвечает.",
                "duration_ms": 10,
                "data": {"alembic_revision": "013_whatsapp_cloud_messages"},
                "recommendation": None,
            },
            {
                "name": "google_drive",
                "status": "WARN",
                "detail": "Service account не состоит в Shared Drive.",
                "duration_ms": 20,
                "data": {"errors": ["no_shared_drive_membership"]},
                "recommendation": "Добавить service account.",
            },
        ],
        "safety": {
            "external_writes_performed": False,
            "secrets_included": False,
            "local_audit_event_written": True,
        },
    }


def test_parse_diagnostic_commands() -> None:
    assert diagnostic_runtime.parse_diagnostic_command("/diag") == (True, None, False)
    assert diagnostic_runtime.parse_diagnostic_command("/diagnostic 107") == (
        True,
        "107",
        False,
    )
    assert diagnostic_runtime.parse_diagnostic_command("диагностика системы 166") == (
        True,
        "166",
        False,
    )
    assert diagnostic_runtime.parse_diagnostic_command("/diag help") == (
        True,
        None,
        True,
    )
    assert diagnostic_runtime.parse_diagnostic_command("покажи проект 107") == (
        False,
        None,
        False,
    )


def test_overall_status() -> None:
    assert _overall_status([{"status": "PASS"}]) == "PASS"
    assert _overall_status([{"status": "PASS"}, {"status": "WARN"}]) == "PASS WITH WARNINGS"
    assert _overall_status([{"status": "WARN"}, {"status": "FAIL"}]) == "FAIL"


def test_secret_fields_are_redacted() -> None:
    value = _safe_value(
        {
            "access_token": "super-secret",
            "database_url": "postgresql://user:password@host/db",
            "nested": {"Authorization": "Bearer abc", "safe": "visible"},
        }
    )
    assert value["access_token"] == "***"
    assert value["database_url"] == "***"
    assert value["nested"]["Authorization"] == "***"
    assert value["nested"]["safe"] == "visible"


def test_reports_contain_trace_and_no_secrets() -> None:
    report = _sample_report()
    summary = format_diagnostic_summary(report)
    markdown = render_diagnostic_markdown(report).decode("utf-8")
    payload = json.loads(render_diagnostic_json(report))

    assert "diag-20260729-230000-abc123" in summary
    assert "google_drive" in summary
    assert "# B&BS System Diagnostic Report" in markdown
    assert payload["trace_id"] == "diag-20260729-230000-abc123"
    assert payload["safety"]["external_writes_performed"] is False


@pytest.mark.asyncio
async def test_run_and_send_exports_markdown_and_json(monkeypatch) -> None:
    report = _sample_report()
    messages: list[str] = []
    documents: list[dict] = []

    async def fake_run(*args, **kwargs):
        assert kwargs["project_query"] == "107"
        return report

    async def fake_send_message(chat_id, text, **kwargs):
        messages.append(text)
        return {"ok": True}

    async def fake_send_document(chat_id, **kwargs):
        documents.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(diagnostic_runtime, "_allowed", lambda: True)
    monkeypatch.setattr(diagnostic_runtime, "run_system_diagnostics", fake_run)
    monkeypatch.setattr(diagnostic_runtime.telegram_service, "send_message", fake_send_message)
    monkeypatch.setattr(diagnostic_runtime.telegram_service, "send_document", fake_send_document)

    reply = await diagnostic_runtime._run_and_send(
        object(),
        chat_id=1,
        telegram_user_id=2,
        project_query="107",
    )

    assert messages and "107" in messages[0]
    assert len(documents) == 2
    assert documents[0]["filename"].endswith(".md")
    assert documents[1]["filename"].endswith(".json")
    assert reply.metadata["trace_id"] == report["trace_id"]
    assert reply.intent == "system_diagnostics"
