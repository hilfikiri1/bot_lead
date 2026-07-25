"""/start command handler."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.bot import messages

router = Router(name="start")


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(messages.START_MESSAGE)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(messages.START_MESSAGE)
