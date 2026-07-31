"""Telegram command/callback wiring for the Facebook lead-intake pipeline.

Kept separate from ``app.api.telegram`` (already several thousand lines) so
the lead-intake surface area is easy to review and test on its own. The
router module below only delegates a few lines into these functions.
"""

from __future__ import annotations

import html
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.lead_processing_job import LeadProcessingJob
from app.services import (
    client_message_service,
    kommo_service,
    phone_utils,
    telegram_service,
    telegram_state_service,
)
from app.services.lead_intake import repository, service, telegram_ui
from app.services.lead_intake.matching import LeadSnapshot
from app.services.lead_intake.schema import LeadQualification

logger = logging.getLogger(__name__)
settings = get_settings()

STATE_MODE_EDIT = "lead_intake_edit_field"

_CALL_OUTCOME_MAP = {
    "call_ok": "connected",
    "call_na": "no_answer",
    "call_later": "rescheduled",
    "call_bad": "wrong_number",
}


async def _send_preview(chat_id: int, result: service.PreviewResult) -> None:
    if result.kind == "preview" and result.job and result.qualification:
        text = telegram_ui.render_preview(
            result.job, snapshot=result.snapshot, qualification=result.qualification
        )
        keyboard = telegram_ui.preview_keyboard(result.job, result.qualification)
        await telegram_service.send_message(chat_id, text, reply_markup=keyboard)
        return
    if result.kind == "manual_match" and result.job:
        text, keyboard = telegram_ui.render_manual_match(
            snapshot=result.snapshot,
            candidates=result.candidates,
            reason=result.message,
            job_id=result.job.id,
        )
        await telegram_service.send_message(chat_id, text, reply_markup=keyboard)
        return
    if result.kind == "error" and result.job:
        text, keyboard = telegram_ui.render_error(result.job)
        await telegram_service.send_message(chat_id, text, reply_markup=keyboard)
        return
    await telegram_service.send_message(
        chat_id, result.message or "Не удалось построить предпросмотр лида."
    )


async def show_next_job(db: AsyncSession, chat_id: int, user_id: int) -> None:
    job = await service.find_or_create_next_job(db)
    if job is None:
        await telegram_service.send_message(chat_id, telegram_ui.no_leads_message())
        return
    job = await repository.save(db, job, telegram_chat_id=chat_id, telegram_user_id=user_id)
    result = await service.build_preview(db, job)
    await _send_preview(chat_id, result)


async def show_job_by_id(db: AsyncSession, chat_id: int, job_id: int) -> None:
    job = await repository.get_by_id(db, job_id)
    if job is None:
        await telegram_service.send_message(chat_id, "Лид не найден.")
        return
    result = await service.build_preview(db, job)
    await _send_preview(chat_id, result)


async def _send_lead_vcard(
    chat_id: int,
    *,
    job: LeadProcessingJob,
    snapshot: LeadSnapshot,
    qualification: LeadQualification | None = None,
) -> None:
    phone = phone_utils.to_e164(snapshot.phone) or str(snapshot.phone or "").strip()
    if not phone:
        return
    product = (
        qualification.product_name_ru
        if qualification and qualification.product_name_ru
        else str((job.raw_snapshot_json or {}).get("product") or "Новый запрос")
    )
    client_name = str(snapshot.name or "Klient").strip()
    display_name = f"{client_name} — {product}"[:120]
    lead_number = str(job.assigned_number or "").strip()
    content = client_message_service.build_vcard(
        name=display_name,
        company=f"B&BS · лид №{lead_number}" if lead_number else "B&BS",
        phone=phone,
        email=str(snapshot.email or "").strip() or None,
        language="pl",
    )
    await telegram_service.send_document(
        chat_id,
        filename=client_message_service.vcard_filename(
            f"{lead_number}_{client_name}_{product}" if lead_number else display_name
        ),
        content=content,
        caption=(
            f"👤 <b>Контакт для iPhone · №{html.escape(lead_number or '—')}</b>\n"
            f"{html.escape(client_name)} — {html.escape(product)}\n"
            f"Телефон: <code>{html.escape(phone_utils.display_phone(phone) or phone)}</code>\n"
            "Нажмите на файл → «Создать новый контакт», затем «Позвонить»."
        ),
        mime_type="text/vcard",
    )


async def _send_call_preparation(
    chat_id: int,
    *,
    job: LeadProcessingJob,
    qualification: LeadQualification,
    snapshot: LeadSnapshot,
) -> None:
    messages, keyboard = telegram_ui.render_call_script(
        qualification, job, snapshot=snapshot
    )
    for index, text in enumerate(messages):
        # Put outcome / dial buttons only on the last chunk.
        markup = keyboard if index == len(messages) - 1 else None
        await telegram_service.send_message(chat_id, text, reply_markup=markup)
    await _send_lead_vcard(
        chat_id, job=job, snapshot=snapshot, qualification=qualification
    )


async def _handle_apply_result(chat_id: int, result: service.ApplyResult) -> None:
    if result.status == "completed":
        qualification = service.qualification_from_job(result.job)
        text, keyboard = telegram_ui.render_completed(result.job, qualification)
        await telegram_service.send_message(chat_id, text, reply_markup=keyboard)
        if qualification and qualification.recommended_action == "phone_call":
            snapshot = service.snapshot_from_job(result.job)
            await _send_lead_vcard(
                chat_id,
                job=result.job,
                snapshot=snapshot,
                qualification=qualification,
            )
        return
    if result.status == "already_completed":
        await telegram_service.send_message(chat_id, "Этот лид уже был обработан ранее.")
        return
    if result.status == "dry_run":
        await telegram_service.send_message(
            chat_id,
            "🧪 DRY RUN — изменения не применяются. Отключите "
            "<code>LEAD_PROCESSING_DRY_RUN</code>, чтобы применить изменения по-настоящему.",
        )
        return
    if result.status == "not_ready":
        await telegram_service.send_message(chat_id, "Лид ещё не готов к применению.")
        return
    text, keyboard = telegram_ui.render_error(result.job)
    await telegram_service.send_message(chat_id, text, reply_markup=keyboard)


async def handle_callback(*, callback_data: str, chat_id: int, user_id: int, db: AsyncSession) -> bool:
    """Handle every ``lp:*`` callback. Returns True once handled (or ignored safely)."""
    if not callback_data.startswith("lp:"):
        return False

    parts = callback_data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "show":
        await show_next_job(db, chat_id, user_id)
        return True

    if action == "show_job" and len(parts) >= 3:
        await show_job_by_id(db, chat_id, int(parts[2]))
        return True

    if action == "pick" and len(parts) >= 4:
        job = await repository.get_by_id(db, int(parts[2]))
        if job is None:
            await telegram_service.send_message(chat_id, "Лид не найден.")
            return True
        try:
            result = await service.select_manual_match(db, job, row_number=int(parts[3]))
        except ValueError:
            await telegram_service.send_message(chat_id, "Строка больше не найдена в таблице.")
            return True
        await _send_preview(chat_id, result)
        return True

    if action == "edit_field" and len(parts) >= 4:
        job_id = int(parts[2])
        field_name = parts[3]
        await telegram_state_service.set_state(
            user_id,
            {"mode": STATE_MODE_EDIT, "chat_id": chat_id, "job_id": job_id, "field": field_name},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id, f"Отправьте новое значение для «{html.escape(field_name)}» следующим сообщением."
        )
        return True

    if len(parts) < 3:
        return True
    job_id = int(parts[2])
    job = await repository.get_by_id(db, job_id)
    if job is None:
        await telegram_service.send_message(chat_id, "Лид не найден или уже обработан.")
        return True

    if action == "apply":
        result = await service.apply_job(db, job_id)
        await _handle_apply_result(chat_id, result)
        if result.status == "completed":
            await show_next_job(db, chat_id, user_id)
        return True

    if action == "skip":
        await service.skip_job(db, job)
        await telegram_service.send_message(
            chat_id, "⏭ Лид пропущен. Kommo и таблица не изменены."
        )
        await show_next_job(db, chat_id, user_id)
        return True

    if action == "edit":
        text, keyboard = telegram_ui.edit_menu(job)
        await telegram_service.send_message(chat_id, text, reply_markup=keyboard)
        return True

    if action == "open":
        try:
            details = await kommo_service.get_lead_details(job.kommo_lead_id)
            url = details.get("url") or f"Kommo ID {job.kommo_lead_id}"
        except Exception:
            url = f"Kommo ID {job.kommo_lead_id}"
        await telegram_service.send_message(chat_id, f"Откройте сделку вручную: {html.escape(str(url))}")
        return True

    if action == "wa":
        qualification = service.qualification_from_job(job)
        if qualification is None:
            await telegram_service.send_message(chat_id, "Сначала постройте предпросмотр лида.")
            return True
        snapshot = service.snapshot_from_job(job)
        text, keyboard = telegram_ui.render_whatsapp_share(job, qualification, snapshot)
        await telegram_service.send_message(chat_id, text, reply_markup=keyboard)
        return True

    if action == "wa_sent":
        result = await service.confirm_whatsapp_sent(db, job_id)
        if result.status == "completed":
            await telegram_service.send_message(
                chat_id, "✅ Отмечено как отправлено. Задача на проверку ответа создана."
            )
        elif result.status == "already_done":
            await telegram_service.send_message(chat_id, "Уже отмечено как отправленное ранее.")
        else:
            await telegram_service.send_message(
                chat_id, f"❌ {html.escape(result.error or 'Не удалось создать задачу.')}"
            )
        return True

    if action == "call":
        qualification = service.qualification_from_job(job)
        if qualification is None:
            await telegram_service.send_message(chat_id, "Сначала постройте предпросмотр лида.")
            return True
        snapshot = service.snapshot_from_job(job)
        await _send_call_preparation(
            chat_id, job=job, qualification=qualification, snapshot=snapshot
        )
        return True

    if action in _CALL_OUTCOME_MAP:
        await service.record_call_result(db, job, outcome=_CALL_OUTCOME_MAP[action])
        await telegram_service.send_message(chat_id, "Результат звонка сохранён в Kommo.")
        return True

    return True


async def handle_text_reply(db: AsyncSession, *, chat_id: int, user_id: int, text: str) -> bool:
    """Consume a free-text reply for the Edit flow. Returns True if it was handled."""
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != STATE_MODE_EDIT:
        return False

    await telegram_state_service.clear_state(user_id)
    job_id = int(state.get("job_id") or 0)
    field_name = str(state.get("field") or "")
    job = await repository.get_by_id(db, job_id)
    if job is None:
        await telegram_service.send_message(chat_id, "Лид не найден.")
        return True

    try:
        job = await service.edit_field(db, job, field_name, text.strip())
    except ValueError as exc:
        await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
        return True

    await telegram_service.send_message(chat_id, "✅ Изменено.")
    result = await service.build_preview(db, job)
    await _send_preview(chat_id, result)
    return True
