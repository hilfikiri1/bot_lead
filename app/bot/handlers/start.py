from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.messages import START_MESSAGE

router = Router()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(START_MESSAGE)
