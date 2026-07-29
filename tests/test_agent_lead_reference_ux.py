"""Production UX tests for internal lead numbers vs Kommo IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import clarification, digest
from app.agent.lead_refs import (
    extract_explicit_kommo_id,
    extract_internal_lead_number,
    parse_lead_references,
    LeadRefType,
    normalize_text,
)
from app.agent.planner import deterministic_plan


ROOT = Path(__file__).resolve().parents[1]


def test_digest_source_shows_internal_and_kommo_id():
    source = (ROOT / "app/agent/digest.py").read_text(encoding="utf-8")
    assert "internal_lead_number" in source
    assert "Kommo ID:" in source


def test_internal_number_from_dash_title():
    lead = {"name": "120 - лампадки"}
    assert extract_internal_lead_number(lead) == "120"


def test_internal_number_from_em_dash_title():
    lead = {"name": "120 — лампадки"}
    assert extract_internal_lead_number(lead) == "120"


def test_small_number_not_used_as_kommo_id_in_tools():
    source = (ROOT / "app/agent/lead_refs.py").read_text(encoding="utf-8")
    assert "INTERNAL_NUMBER_MAX" in source
    assert "resolve_lead_for_plan" in source
    tools = (ROOT / "app/agent/tools.py").read_text(encoding="utf-8")
    assert "resolve_lead_for_plan" in tools


def test_explicit_kommo_id_resolves():
    assert extract_explicit_kommo_id("Kommo ID 12345678") == 12345678


def test_explicit_hash_kommo_id_resolves():
    assert extract_explicit_kommo_id("добавь в #12345678: тест") == 12345678


def test_digest_position_eighth():
    refs = parse_lead_references("по восьмому завтра звонок", {})
    assert any(r.ref_type == LeadRefType.DIGEST_POSITION and r.digest_position == 8 for r in refs)


def test_internal_number_by_120():
    refs = parse_lead_references("по 120 завтра позвонить", {})
    assert any(
        r.ref_type == LeadRefType.INTERNAL_NUMBER and r.internal_lead_number == "120"
        for r in refs
    )


def test_digest_position_not_same_as_internal_number():
    refs_position = parse_lead_references("по восьмому", {})
    refs_internal = parse_lead_references("по 8", {})
    assert any(r.digest_position == 8 for r in refs_position)
    assert not any(r.digest_position == 8 for r in refs_internal)
    assert any(r.internal_lead_number == "8" for r in refs_internal)


def test_pending_clarification_structure():
    source = (ROOT / "app/agent/clarification.py").read_text(encoding="utf-8")
    assert "pending_clarification" in source or "build_pending" in source
    assert "original_intent" in source
    assert "unresolved_references" in source


def test_short_answer_continues_via_continue_pending():
    source = (ROOT / "app/agent/service.py").read_text(encoding="utf-8")
    assert "continue_pending" in source
    assert "pre_resolved_leads" in source


def test_batch_numbers_parsed():
    refs = parse_lead_references("107 83 117", {})
    numbers = [r.internal_lead_number for r in refs if r.internal_lead_number]
    assert "107" in numbers
    assert "83" in numbers
    assert "117" in numbers


def test_due_at_continues_batch_command():
    assert clarification.looks_like_due_at("завтра в 10")


def test_batch_task_requires_confirmation():
    source = (ROOT / "app/agent/executor.py").read_text(encoding="utf-8")
    assert "create_kommo_tasks_batch" in source
    service = (ROOT / "app/agent/service.py").read_text(encoding="utf-8")
    assert "stage_action" in service
    assert "create_kommo_tasks_batch" in service


def test_batch_note_requires_confirmation():
    source = (ROOT / "app/agent/executor.py").read_text(encoding="utf-8")
    assert "add_kommo_notes_batch" in source


def test_double_confirm_idempotent():
    source = (ROOT / "app/agent/executor.py").read_text(encoding="utf-8")
    assert "уже было выполнено" in source
    assert "уже выполнено" in source


def test_partial_batch_failure_supported():
    source = (ROOT / "app/agent/executor.py").read_text(encoding="utf-8")
    assert "partial_failed" in source
    assert "_batch_has_partial_success" in source


def test_batch_retry_skips_successful_items():
    source = (ROOT / "app/agent/executor.py").read_text(encoding="utf-8")
    assert "уже выполнено" in source
    assert "item_results" in source


def test_cancel_clears_pending():
    assert clarification.is_cancel_command("/cancel")
    assert clarification.is_cancel_command("отмена")


def test_menu_clears_pending_in_service():
    source = (ROOT / "app/agent/service.py").read_text(encoding="utf-8")
    assert "is_menu_command" in source
    assert "clear_pending" in source


def test_single_lead_commands_still_work():
    plan = deterministic_plan("добавь примечание в #123456: клиент ждёт цену", {})
    assert plan is not None
    assert plan.intent == "add_kommo_note"
    assert plan.lead_id == 123456


def test_voice_and_text_share_resolver():
    voice = (ROOT / "app/tasks/voice_note_tasks.py").read_text(encoding="utf-8")
    assert "agent_service.handle_message" in voice
    service = (ROOT / "app/agent/service.py").read_text(encoding="utf-8")
    assert "lead_refs" in service or "resolve_leads" in service


def test_dates_not_parsed_as_lead_list():
    refs = parse_lead_references("По 120 позвонить 30 июля в 10:00", {})
    numbers = [r.internal_lead_number for r in refs if r.internal_lead_number]
    assert numbers == ["120"]
    assert "30" not in numbers
    assert "10" not in numbers


def test_candidate_buttons_set_active_lead():
    service = (ROOT / "app/agent/service.py").read_text(encoding="utf-8")
    tools = (ROOT / "app/agent/tools.py").read_text(encoding="utf-8")
    assert "set_active_lead" in service
    assert "agent:lead:" in tools
    assert "lead_card_actions_markup" in tools


def test_digest_map_saved_to_context():
    source = (ROOT / "app/agent/service.py").read_text(encoding="utf-8")
    assert "last_digest" in source
    assert "build_last_digest_context" in source


def test_batch_preview_in_planner():
    plan = deterministic_plan("поставь задачи по 107, 83 и 117", {})
    assert plan is not None
    assert plan.intent == "create_kommo_tasks_batch"


def test_digest_markup_has_select_buttons():
    markup = digest.digest_markup(
        [
            {
                "position": 8,
                "kommo_lead_id": 19873456,
                "internal_lead_number": "120",
                "name": "Лампадки",
            }
        ]
    )
    assert markup is not None
    button = markup["inline_keyboard"][0][0]
    assert button["callback_data"] == "agent:digest:8"
    assert "120" in button["text"]
