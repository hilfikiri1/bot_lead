"""Add-talk button on deal card and free-standing talk→lead picker."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.agent import project_snapshot
from app.api import telegram as telegram_api


def test_deal_card_has_add_talk_button_bound_to_lead():
    snapshot = project_snapshot.ProjectSnapshot(
        identity={"kommo_lead_id": 169},
        kommo={"url": "https://kommo.test/169"},
    )
    markup = project_snapshot.project_actions_markup(snapshot)
    talk = next(
        button
        for row in markup["inline_keyboard"]
        for button in row
        if button.get("text") == "🎙 Добавить разговор"
    )
    assert talk["callback_data"] == "lead:audio:169:0"


@pytest.mark.asyncio
async def test_menu_talk_asks_for_deal_first():
    with (
        patch(
            "app.api.telegram.telegram_state_service.set_state",
            new=AsyncMock(),
        ) as set_state,
        patch(
            "app.api.telegram.telegram_service.send_message",
            new=AsyncMock(),
        ) as send_message,
    ):
        await telegram_api._prompt_talk_lead_pick(chat_id=1, user_id=7)

    set_state.assert_awaited()
    assert set_state.await_args.args[1]["mode"] == "awaiting_talk_lead_search"
    assert "к какой сделке" in send_message.await_args.args[1].casefold()


@pytest.mark.asyncio
async def test_talk_lead_search_single_match_starts_audio_wait():
    state = {"mode": "awaiting_talk_lead_search", "chat_id": 1}
    with (
        patch(
            "app.api.telegram.telegram_state_service.get_state",
            new=AsyncMock(return_value=state),
        ),
        patch(
            "app.api.telegram.telegram_state_service.clear_state",
            new=AsyncMock(),
        ),
        patch(
            "app.api.telegram.kommo_service.search_open_leads",
            new=AsyncMock(
                return_value={
                    "leads": [{"id": 169, "name": "169 - Сельхозтовары"}],
                }
            ),
        ),
        patch(
            "app.api.telegram._prompt_followup_audio",
            new=AsyncMock(),
        ) as prompt_audio,
        patch(
            "app.api.telegram.telegram_service.send_message",
            new=AsyncMock(),
        ),
        patch(
            "app.api.telegram.identity_service.current_user",
            return_value=None,
        ),
    ):
        handled = await telegram_api._handle_text_state(
            chat_id=1, user_id=7, text="169", db=AsyncMock()
        )

    assert handled is True
    prompt_audio.assert_awaited_once_with(1, 7, 169, return_page=0)


@pytest.mark.asyncio
async def test_talk_pick_callback_starts_audio_for_selected_lead():
    with patch(
        "app.api.telegram._prompt_followup_audio",
        new=AsyncMock(),
    ) as prompt_audio:
        handled = await telegram_api._handle_manager_callback(
            callback_data="talk:pick:169",
            chat_id=1,
            user_id=7,
            db=AsyncMock(),
        )
    assert handled is True
    prompt_audio.assert_awaited_once_with(1, 7, 169, return_page=0)


@pytest.mark.asyncio
async def test_lead_view_while_picking_talk_starts_audio():
    with (
        patch(
            "app.api.telegram.telegram_state_service.get_state",
            new=AsyncMock(
                return_value={"mode": "awaiting_talk_lead_search", "chat_id": 1}
            ),
        ),
        patch(
            "app.api.telegram.telegram_state_service.clear_state",
            new=AsyncMock(),
        ) as clear_state,
        patch(
            "app.api.telegram._prompt_followup_audio",
            new=AsyncMock(),
        ) as prompt_audio,
        patch(
            "app.api.telegram._show_lead_details",
            new=AsyncMock(),
        ) as show_details,
    ):
        handled = await telegram_api._handle_manager_callback(
            callback_data="lead:view:169:2",
            chat_id=1,
            user_id=7,
            db=AsyncMock(),
        )
    assert handled is True
    clear_state.assert_awaited_once_with(7)
    prompt_audio.assert_awaited_once_with(1, 7, 169, return_page=2)
    show_details.assert_not_awaited()
