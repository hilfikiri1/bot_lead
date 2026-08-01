from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent import memory


@pytest.mark.asyncio
async def test_build_context_preserves_bug_intake_for_next_message():
    session = SimpleNamespace(
        active_kommo_lead_id=None,
        active_local_lead_id=None,
        memory_summary=None,
        last_intent="qa_intake_started",
        context={
            "qa_intake": {
                "issue_type": "Bug",
                "started_at": "2026-07-31T15:00:00+00:00",
            },
            "active_lead_name": "166 - Чай",
        },
    )
    db = AsyncMock()

    with (
        patch.object(
            memory.crm_service,
            "get_user_command_context",
            new=AsyncMock(
                return_value={
                    "kommo_lead_id": 166,
                    "lead_name": "166 - Чай",
                }
            ),
        ),
        patch.object(memory, "recent_messages", new=AsyncMock(return_value=[])),
    ):
        context = await memory.build_context(
            db,
            telegram_user_id=100,
            session=session,
        )

    assert context["qa_intake"]["issue_type"] == "Bug"
    assert context["active_kommo_lead_id"] == 166
    assert context["last_intent"] == "qa_intake_started"


@pytest.mark.asyncio
async def test_build_context_preserves_bug_retest_state():
    session = SimpleNamespace(
        active_kommo_lead_id=None,
        active_local_lead_id=None,
        memory_summary=None,
        last_intent="qa_retest_started",
        context={"qa_retest_issue_id": 42},
    )
    db = AsyncMock()

    with (
        patch.object(
            memory.crm_service,
            "get_user_command_context",
            new=AsyncMock(return_value={}),
        ),
        patch.object(memory, "recent_messages", new=AsyncMock(return_value=[])),
    ):
        context = await memory.build_context(
            db,
            telegram_user_id=100,
            session=session,
        )

    assert context["qa_retest_issue_id"] == 42
    assert context["qa_intake"] is None
