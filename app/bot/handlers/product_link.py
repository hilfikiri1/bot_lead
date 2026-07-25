from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import FSInputFile, Message

from app.bot import messages
from app.exceptions import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
    InvalidProductUrlError,
    OpenAIProcessingError,
    ProductDataNotFoundError,
    ProductPageNotFoundError,
)
from app.services.task_service import TaskService

router = Router()
task_service = TaskService()


@router.message(F.text)
async def handle_product_link(message: Message, bot: Bot) -> None:
    if not message.text:
        return

    status = await message.answer(messages.LINK_RECEIVED)
    try:
        result = await task_service.process_link(
            telegram_user_id=message.from_user.id if message.from_user else 0,
            telegram_chat_id=message.chat.id,
            raw_link=message.text.strip(),
            update_status=lambda text: status.edit_text(text),
        )
    except InvalidProductUrlError:
        await status.edit_text(messages.INVALID_LINK_SHORT)
        return
    except (AuthenticationRequiredError, CaptchaDetectedError):
        await status.edit_text(messages.CAPTCHA_REQUIRED)
        return
    except ProductPageNotFoundError:
        await status.edit_text(messages.PAGE_UNAVAILABLE)
        return
    except ProductDataNotFoundError:
        await status.edit_text(messages.NOT_ENOUGH_DATA)
        return
    except OpenAIProcessingError:
        await status.edit_text(messages.TEMPORARY_ERROR)
        return
    except RuntimeError as exc:
        if str(exc) == messages.ALREADY_RUNNING:
            await status.edit_text(messages.ALREADY_RUNNING)
        else:
            await status.edit_text(messages.TEMPORARY_ERROR)
        return

    await status.edit_text(messages.STATUS_DONE)
    await bot.send_document(
        message.chat.id,
        FSInputFile(result.pdf_path),
        caption=messages.PDF_CAPTION,
    )
