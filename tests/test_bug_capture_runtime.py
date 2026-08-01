from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from app.agent import service as agent_service
from app.services import bug_capture_runtime, goals_qa_service, qa_projection_runtime


def _issue() -> SimpleNamespace:
    return SimpleNamespace(
        id=12,
        issue_code="BUG-0012",
        title="После /bug текст уходит CRM-агенту",
        issue_type="Bug",
        priority="High",
        module="Telegram",
        environment="production",
        description="Нажал /bug, затем отправил описание, но бот показал рекомендации по сделке.",
        actual_result="Бот обработал описание как обычную команду.",
        expected_result="Создать карточку бага.",
        reproduction_steps="1. /bug\n2. Отправить описание",
        user_comment=None,
        notion_url="https://notion.test/bug-12",
        attachments=[
            SimpleNamespace(
                upload_status="uploaded",
                drive_url="https://drive.test/screenshot-12",
                metadata_json={
                    "drive_folder_url": "https://drive.test/folder-12"
                },
            )
        ],
    )


def test_bug_fix_prompt_contains_record_and_links():
    prompt = bug_capture_runtime.build_bug_fix_prompt(_issue())

    assert "BUG-0012" in prompt
    assert "После /bug текст уходит CRM-агенту" in prompt
    assert "Создать карточку бага" in prompt
    assert "https://drive.test/screenshot-12" in prompt
    assert "https://notion.test/bug-12" in prompt
    assert "Не проводи полный аудит проекта" in prompt


@pytest.mark.asyncio
async def test_bug_folder_is_created_under_bugs_year_month():
    issue = _issue()
    ensure = AsyncMock(
        side_effect=[
            {"id": "bugs"},
            {"id": "year"},
            {"id": "month"},
            {"id": "issue", "webViewLink": "https://drive.test/issue"},
        ]
    )

    with (
        patch.dict("os.environ", {"GOOGLE_DRIVE_QA_FOLDER_ID": "root"}),
        patch.object(qa_projection_runtime, "_ensure_folder", new=ensure),
    ):
        folder = await bug_capture_runtime._ensure_bug_folder(issue)

    assert folder["id"] == "issue"
    assert ensure.await_args_list[0] == call("Баги", "root")
    assert ensure.await_args_list[1].args[1] == "bugs"
    assert ensure.await_args_list[2].args[1] == "year"
    assert ensure.await_args_list[3] == call("BUG-0012", "month")


@pytest.mark.asyncio
async def test_bug_prompt_command_returns_copy_ready_prompt():
    bug_capture_runtime.install_bug_capture_runtime()
    issue = _issue()

    with patch.object(
        goals_qa_service,
        "get_issue",
        new=AsyncMock(return_value=issue),
    ):
        reply = await agent_service.handle_message(
            AsyncMock(),
            chat_id=20,
            telegram_user_id=10,
            text="/bug_prompt BUG-0012",
        )

    assert reply.intent == "bug_prompt_ready"
    assert "Готовый промпт" in reply.text
    assert "BUG-0012" in reply.text
    assert "drive.test/screenshot-12" in reply.text
