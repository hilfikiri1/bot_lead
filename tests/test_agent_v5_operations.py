"""Agent v5.0 — Digital Operations Director regression and unit tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.planner import deterministic_plan
from app.main import APP_VERSION
from app.services import (
    calendar_policy,
    contact_resolver,
    conversation_analysis_service,
    document_intelligence_service,
    drive_diagnostics,
    lead_assessment_service,
    next_action_service,
    sheets_analytics_service,
    unified_project_service,
)
from app.services.contact_resolver import resolve_contact
from app.services.drive_diagnostics import DriveErrorInfo, classify_drive_exception
from app.services.google_drive_service import GoogleDriveError
from app.services.sheets_analytics_service import apply_number_to_title, extract_title_number


def _lead_117_like() -> dict:
    """Synthetic lead shaped like production №117 — not hardcoded as special-case logic."""
    return {
        "id": 10535709,
        "name": "117 — Урны",
        "price": 25000,
        "status_name": "В работе",
        "pipeline_name": "B2B",
        "updated_at": 1_700_000_000,
        "closest_task_at": 1_800_000_000,
        "contacts": [
            {
                "id": 555,
                "name": "Anna Mosińska",
                "phones": ["+48 501 407 028"],
                "emails": ["anna@example.com"],
            }
        ],
        "notes": [{"text": "Клиент ждёт расчёт", "created_at": 1_700_000_100}],
    }


def test_app_version_is_5_0_0():
    assert APP_VERSION == "5.0.0"
    source = Path("app/main.py").read_text(encoding="utf-8")
    assert 'APP_VERSION = "5.0.0"' in source
    assert "/ready" in source
    assert "/version" in source


def test_contact_resolver_uses_linked_contact_phone():
    lead = _lead_117_like()
    contact = resolve_contact(lead)
    assert contact.name == "Anna Mosińska"
    assert contact.phone_normalized == "48501407028"
    assert contact.phone_display is not None
    assert "501" in contact.phone_display
    assert contact.source == "kommo_contact"
    assert contact_resolver.whatsapp_url(contact.phone_normalized) == "https://wa.me/48501407028"


def test_contact_resolver_does_not_warn_when_phone_present():
    lead = _lead_117_like()
    contact = resolve_contact(lead)
    assert contact.phone_normalized
    # Unified formatter must not invent a missing-phone warning for this shape.
    project = unified_project_service.UnifiedProject(
        primary_contact=contact,
        whatsapp_url=contact_resolver.whatsapp_url(contact.phone_normalized),
        missing_information=[],
    )
    text = unified_project_service.format_unified_project(project)
    assert "нет телефона" not in text.casefold()
    assert "WhatsApp" in text or "wa.me" in text


def test_contact_resolver_falls_back_to_lead_fields():
    lead = {
        "id": 1,
        "name": "Только сделка",
        "contacts": [],
        "custom_fields": {"phone": "+380501112233"},
    }
    contact = resolve_contact(lead)
    assert contact.phone_normalized == "380501112233"
    assert contact.source == "kommo_lead_fields"


def test_drive_403_is_classified_not_generic_only():
    err = GoogleDriveError("Нет доступа", status_code=403)
    info = classify_drive_exception(err)
    assert isinstance(info, DriveErrorInfo)
    assert info.category in {
        "no_folder_access",
        "api_disabled",
        "no_shared_drive_membership",
        "quota_exceeded",
        "auth_failed",
    }
    assert "категор" in drive_diagnostics.format_drive_status(
        {"checks": [], "errors": [info.category]}
    ).casefold() or "Категории" in drive_diagnostics.format_drive_status(
        {"checks": [], "errors": [info.category]}
    )


def test_drive_status_command_planned():
    plan = deterministic_plan("/drive_status", {})
    assert plan is not None
    assert plan.intent == "drive_status"


def test_planner_v5_commands():
    for text, intent in (
        ("/plan", "daily_plan"),
        ("/inbox", "project_inbox"),
        ("/overdue", "overdue_actions"),
        ("/without_next", "without_next_action"),
        ("/waiting_client", "waiting_client"),
        ("/waiting_us", "waiting_us"),
        ("/stale", "stale_projects"),
        ("/integration_status", "integration_status"),
        ("/sheets_sync_preview", "sheets_sync_preview"),
        ("/history 135", "project_history"),
        ("оценка лида", "lead_assessment"),
    ):
        plan = deterministic_plan(text, {"active_kommo_lead_id": 1})
        assert plan is not None, text
        assert plan.intent == intent, text


def test_next_action_overdue_and_without_next():
    overdue = next_action_service.evaluate_lead_next_action(
        {"id": 1, "name": "A", "closest_task_at": 100, "updated_at": 50},
        now=__import__("datetime").datetime.fromtimestamp(200, tz=__import__("datetime").timezone.utc),
    )
    assert overdue.status == "overdue"
    missing = next_action_service.evaluate_lead_next_action(
        {"id": 2, "name": "B", "closest_task_at": None, "updated_at": 190},
        now=__import__("datetime").datetime.fromtimestamp(200, tz=__import__("datetime").timezone.utc),
    )
    assert missing.status == "missing"


def test_lead_assessment_grades():
    strong = lead_assessment_service.assess_lead(_lead_117_like())
    assert strong.grade in {"A", "B"}
    assert strong.score >= 40
    weak = lead_assessment_service.assess_lead({"id": 9, "name": "x", "contacts": []})
    assert weak.grade in {"B", "C"}


def test_calendar_policy_followup_vs_timed_call():
    assert calendar_policy.requires_calendar(event_type="call", due_at="завтра в 14:00") is True
    assert calendar_policy.requires_calendar(event_type="followup", due_at="завтра") is False
    assert calendar_policy.requires_calendar(event_type="message", due_at="в пятницу") is False


def test_sheets_numbering_idempotent():
    assert extract_title_number("166 CH — станок") == "166"
    assert apply_number_to_title("166 CH — станок", "166") == "166 CH — станок"
    assert apply_number_to_title("станок для клиента", "166").startswith("166 ")
    preview = sheets_analytics_service.build_sheets_sync_preview(
        leads=[{"id": 10, "name": "товар", "contacts": [{"phones": ["+48111111111"]}]}],
        sheet_rows=[{"row": 2, "phone": "+48 111 111 111", "lead_number": ""}],
    )
    assert preview.number_assignments
    assert preview.number_assignments[0]["internal_number"]


def test_document_hash_and_classification():
    payload = b"%PDF-1.4 demo"
    digest = document_intelligence_service.content_hash(payload)
    assert len(digest) == 64
    assert document_intelligence_service.classify_document(filename="offer.pdf", mime_type="application/pdf") in {
        "commercial_offer",
        "other",
    }
    extracted = document_intelligence_service.extract_document_fields("Price FOB USD 1200 MOQ 500")
    assert extracted["fields"].get("incoterms") == "FOB"
    assert extracted["fields"].get("moq") == "500"


def test_conversation_analysis_marks_uncertainty():
    result = conversation_analysis_service.analyze_conversation_text("ок")
    assert result.uncertain
    assert "Не могу достоверно определить" in result.uncertain[0]


def test_migration_011_exists():
    path = Path("migrations/versions/011_agent_v5_operations.py")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "011_agent_v5_operations" in text
    assert "project_events" in text
    assert "integration_operations" in text
    assert 'down_revision = "010_agent_v4_2_workspace"' in text


def test_executor_and_services_wired():
    service = Path("app/agent/service.py").read_text(encoding="utf-8")
    assert "unified_project_service" in service
    assert "drive_diagnostics" in service
    assert "next_action_service" in service
    assert "project_timeline_service" in service
    drive = Path("app/services/google_drive_service.py").read_text(encoding="utf-8")
    assert "classify_http_error" in drive


@pytest.mark.asyncio
async def test_outbox_enqueue_idempotent():
    from unittest.mock import MagicMock

    from app.services import outbox_service

    existing = SimpleNamespace(
        id=1,
        idempotency_key="abc",
        status="pending",
        operation_type="drive_upload",
        service="drive",
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    row = await outbox_service.enqueue(
        db,
        operation_type="drive_upload",
        service="drive",
        payload={},
        idempotency_key="abc",
    )
    assert row is existing
