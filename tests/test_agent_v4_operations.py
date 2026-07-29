"""Agent v4 operations: Drive projects, snapshots, digests, costs and file uploads."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import digest, project_drive, project_linking, project_snapshot
from app.agent.planner import deterministic_plan
from app.services import project_link_service
from app.services.ai_usage_service import estimate_cost_usd, format_costs_report
from app.services.agent_scheduled_digest_service import _digest_idempotency_key
from app.services.google_drive_service import PROJECT_SUBFOLDERS, sanitize_filename


def test_build_project_key_with_internal_number():
    key = project_link_service.build_project_key(
        country_code="PL",
        internal_lead_number="120",
        kommo_lead_id=999,
    )
    assert key == "BBS-PL-0120"


def test_build_project_key_without_internal_number():
    key = project_link_service.build_project_key(
        country_code="UA",
        internal_lead_number=None,
        kommo_lead_id=12345678,
    )
    assert key == "BBS-UA-KOMMO-12345678"


def test_infer_country_from_phone_prefix():
    lead = {"contacts": [{"phones": ["+48123456789"]}]}
    assert project_link_service.infer_country_code(lead=lead) == "PL"


def test_planner_create_drive_project_intent():
    plan = deterministic_plan("создай проект в drive по сделке 120", {"active_kommo_lead_id": 1})
    assert plan is not None
    assert plan.intent == "create_drive_project"
    assert plan.mode == "write"


def test_planner_link_project_systems_intent():
    plan = deterministic_plan("свяжи notion с проектом", {"active_kommo_lead_id": 42})
    assert plan is not None
    assert plan.intent == "link_project_systems"


def test_planner_project_snapshot_intent():
    plan = deterministic_plan("что происходит по проекту", {"active_kommo_lead_id": 7})
    assert plan is not None
    assert plan.intent == "project_snapshot"


def test_planner_ai_costs_intent():
    plan = deterministic_plan("/costs", {})
    assert plan is not None
    assert plan.intent == "ai_costs"


def test_digest_sections_group_urgent_and_planned():
    digest_map = [
        {"position": 1, "score": 100, "name": "A"},
        {"position": 2, "score": 20, "name": "B"},
    ]
    sections = digest.group_digest_sections(digest_map)
    assert len(sections["urgent"]) == 1
    assert len(sections["planned"]) == 1


def test_format_digest_includes_section_headers():
    result = {
        "open_count": 2,
        "digest_map": [
            {
                "position": 1,
                "score": 95,
                "internal_lead_number": "120",
                "kommo_lead_id": 1,
                "name": "Лампадки",
                "priority": "Высокий",
                "reason": "просрочена задача",
                "next_step": "Позвонить",
                "url": "https://kommo.test/1",
            }
        ],
        "sections": digest.group_digest_sections(
            [
                {
                    "position": 1,
                    "score": 95,
                    "internal_lead_number": "120",
                    "kommo_lead_id": 1,
                    "name": "Лампадки",
                    "priority": "Высокий",
                    "reason": "просрочена задача",
                    "next_step": "Позвонить",
                    "url": "https://kommo.test/1",
                }
            ]
        ),
    }
    text = digest.format_digest(result)
    assert "Срочно" in text
    assert "№120" in text


def test_drive_preview_lists_subfolders():
    data = {
        "project_key": "BBS-PL-0120",
        "internal_lead_number": "120",
        "kommo_lead_id": 1,
        "kommo_lead_name": "120 - тест",
        "client_name": "Клиент",
        "country_code": "PL",
        "folder_name": "BBS-PL-0120 — тест",
        "subfolders": list(PROJECT_SUBFOLDERS[:3]),
        "warnings": [],
    }
    text = project_drive.format_drive_project_preview(data)
    assert "BBS-PL-0120" in text
    assert PROJECT_SUBFOLDERS[0] in text
    assert "подтверждения" in text.casefold()


def test_link_preview_mentions_notion_and_drive():
    data = {
        "project_key": "BBS-PL-0120",
        "internal_lead_number": "120",
        "kommo_lead_name": "Тест",
        "notion_url": "https://notion.so/page",
        "drive_folder_url": "https://drive.google.com/folder",
        "warnings": [],
    }
    text = project_linking.format_link_preview(data)
    assert "Notion" in text
    assert "Drive" in text


def test_snapshot_format_includes_links():
    snap = project_snapshot.ProjectSnapshot(
        identity={"project_key": "BBS-PL-0120", "internal_lead_number": "120"},
        client={"name": "Клиент"},
        kommo={"name": "Сделка", "status": "Новый", "price": "1000", "url": "https://kommo.test/1"},
        notion={"url": "https://notion.so/p"},
        drive={"url": "https://drive.google.com/f"},
        recommended_next_action="Создать КП",
    )
    text = project_snapshot.format_snapshot(snap)
    assert "BBS-PL-0120" in text
    assert "Kommo" in text
    assert "Notion" in text
    assert "Drive" in text


def test_sanitize_filename_strips_unsafe_chars():
    assert sanitize_filename("bad/name<>test.pdf") == "bad_name_test.pdf"


def test_ai_cost_estimate_and_report():
    cost = estimate_cost_usd(model="gpt-4o-mini", input_tokens=1000, output_tokens=500)
    assert cost is not None
    assert cost > Decimal("0")
    report = format_costs_report(
        {
            "today_cost_usd": 0.01,
            "month_cost_usd": 0.5,
            "top_operations": [{"operation": "planner", "count": 3, "cost_usd": 0.5}],
            "warnings": [],
        }
    )
    assert "AI usage" in report
    assert "planner" in report


def test_scheduled_digest_idempotency_key():
    key = _digest_idempotency_key("morning", 99, "2026-07-29")
    assert key == "digest:morning:99:2026-07-29"


@pytest.mark.asyncio
async def test_handle_project_file_upload_stages_action():
    from app.agent import service as agent_service

    link = SimpleNamespace(
        project_key="BBS-PL-0120",
        drive_folder_id="folder123",
    )
    with (
        patch.object(agent_service.memory, "get_or_create_session", new=AsyncMock(return_value=SimpleNamespace())),
        patch.object(agent_service.memory, "build_context", new=AsyncMock(return_value={"active_kommo_lead_id": 42})),
        patch.object(agent_service.kommo_service, "get_lead_details", new=AsyncMock(return_value={"id": 42, "name": "Тест"})),
        patch("app.services.project_link_service.get_by_kommo_lead_id", new=AsyncMock(return_value=link)),
        patch.object(agent_service.storage_service, "save_project_file", new=AsyncMock(return_value="/tmp/file.pdf")),
        patch.object(agent_service.actions, "stage_action", new=AsyncMock(return_value=SimpleNamespace(id=7))),
    ):
        reply = await agent_service.handle_project_file_upload(
            AsyncMock(),
            chat_id=1,
            telegram_user_id=99,
            filename="offer.pdf",
            mime_type="application/pdf",
            content=b"%PDF-1.4",
        )
    assert "Загрузить файл" in reply.text
    assert reply.reply_markup is not None


def test_executor_has_v4_action_handlers():
    source = Path("app/agent/executor.py").read_text(encoding="utf-8")
    for action in (
        "create_drive_project",
        "link_project_systems",
        "save_file_to_drive_project",
    ):
        assert f'action_type == "{action}"' in source


def test_telegram_accepts_project_file_uploads():
    source = Path("app/api/telegram.py").read_text(encoding="utf-8")
    assert "_extract_project_file_attachment" in source
    assert "handle_project_file_upload" in source
