"""Telegram preview/keyboard rendering for the lead-intake approval flow.

Uses the existing ``app.services.telegram_service`` transport
(``send_message``/``answer_callback_query``) — no new Telegram client.
"""

from __future__ import annotations

import html
from typing import Any

from app.models.lead_processing_job import LeadProcessingJob
from app.services import phone_utils
from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_intake.matching import LeadSnapshot
from app.services.lead_intake.schema import LeadQualification

_POTENTIAL_RU = {"high": "высокий", "medium": "средний", "low": "низкий", "unknown": "не определён"}
_READINESS_RU = {"high": "высокая", "medium": "средняя", "low": "низкая", "unknown": "не определена"}
_ACTION_RU = {
    "whatsapp": "Написать в WhatsApp",
    "phone_call": "Позвонить клиенту",
    "email": "Написать на email",
    "manual_review": "Ручная проверка менеджером",
}
_REASON_LABEL_RU = {
    "duplicate_phone": "в таблице несколько строк с этим телефоном",
    "duplicate_email": "в таблице несколько строк с этим email",
    "no_matching_row": "не найдена подходящая строка в таблице",
    "conflicting_facebook_id": "разные строки ссылаются на один Facebook Lead ID",
    "assigned_number_conflict": "номер уже используется другим лидом",
    "missing_required_fields": "у лида нет телефона, email и Facebook Lead ID",
}


def _esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def dry_run_banner() -> str:
    return "🧪 <b>DRY RUN — изменения не применяются</b>\n\n"


def render_preview(
    job: LeadProcessingJob,
    *,
    snapshot: LeadSnapshot,
    qualification: LeadQualification,
) -> str:
    phone_display = phone_utils.display_phone(snapshot.phone) or _esc(snapshot.phone)
    proposed_name = f"{job.assigned_number} - {qualification.product_name_ru}"

    lines: list[str] = []
    if job.dry_run:
        lines.append(dry_run_banner().rstrip())
        lines.append("")
    lines.extend(
        [
            "🆕 <b>НОВЫЙ ЛИД</b>",
            "",
            "<b>Предлагаемое название:</b>",
            _esc(proposed_name),
            "",
            "<b>Клиент:</b>",
            _esc(snapshot.name),
            "",
            "<b>Телефон:</b>",
            _esc(phone_display),
            "",
            "<b>Email:</b>",
            _esc(snapshot.email),
            "",
            "<b>Регион:</b>",
            _esc(snapshot.region),
            "",
            "<b>Товар из заявки:</b>",
            _esc(snapshot.product),
            "",
            "<b>Перевод:</b>",
            _esc(qualification.product_name_ru),
            "",
            "<b>ОЦЕНКА</b>",
            "",
            f"Потенциал: {_POTENTIAL_RU.get(qualification.potential, qualification.potential)}",
            f"Готовность: {_READINESS_RU.get(qualification.readiness, qualification.readiness)}",
            f"Приоритет: {_esc(qualification.priority)} — {_esc(qualification.priority_label_ru)}",
            "",
            "<b>Рекомендуемое действие:</b>",
            _esc(_ACTION_RU.get(qualification.recommended_action, qualification.recommended_action)),
            "",
            "<b>Почему:</b>",
            _esc(qualification.recommended_action_reason_ru),
        ]
    )

    if qualification.missing_information_ru:
        lines.extend(["", "<b>ЧТО НУЖНО УТОЧНИТЬ</b>", ""])
        lines.extend(f"• {_esc(item)};" for item in qualification.missing_information_ru)

    if qualification.recommended_action == "whatsapp":
        lines.extend(["", "<b>ГОТОВОЕ СООБЩЕНИЕ</b>", "", _esc(qualification.client_message.text)])
    elif qualification.recommended_action == "phone_call" and qualification.call_script:
        lines.extend(
            [
                "",
                "<b>ЦЕЛЬ ЗВОНКА</b>",
                "",
                _esc(qualification.call_script.objective),
            ]
        )

    lines.extend(
        [
            "",
            "<b>ПЛАН ИЗМЕНЕНИЙ</b>",
            "",
            f"1. Записать ID {_esc(job.assigned_number)} в Google Sheets.",
            f"2. Переименовать сделку в «{_esc(proposed_name)}».",
            "3. Перевести сделку на этап «Первый контакт».",
            "4. Добавить примечание с анализом.",
            f"5. Создать задачу «{_esc(qualification.task.title_ru)}».",
        ]
    )
    if qualification.recommended_action == "whatsapp":
        lines.append("6. Сохранить готовое сообщение WhatsApp.")

    if job.dry_run:
        lines.extend(["", "🧪 Apply в dry-run режиме ничего не запишет во внешние системы."])

    return "\n".join(lines)[:4000]


def preview_keyboard(job: LeadProcessingJob, qualification: LeadQualification) -> dict[str, Any]:
    apply_label = "🧪 Apply (dry-run)" if job.dry_run else "✅ Apply"
    rows: list[list[dict[str, str]]] = [
        [
            {"text": apply_label, "callback_data": f"lp:apply:{job.id}"},
            {"text": "✏️ Edit", "callback_data": f"lp:edit:{job.id}"},
            {"text": "⏭ Skip", "callback_data": f"lp:skip:{job.id}"},
        ]
    ]
    if qualification.recommended_action == "whatsapp":
        rows.append([{"text": "📲 Send WhatsApp", "callback_data": f"lp:wa:{job.id}"}])
    elif qualification.recommended_action == "phone_call":
        rows.append([{"text": "📞 Prepare call", "callback_data": f"lp:call:{job.id}"}])
    return {"inline_keyboard": rows}


def render_manual_match(
    *, snapshot: LeadSnapshot, candidates: list[SpreadsheetRow], reason: str | None, job_id: int
) -> tuple[str, dict[str, Any]]:
    phone_display = phone_utils.display_phone(snapshot.phone) or _esc(snapshot.phone)
    lines = [
        "⚠️ <b>Multiple possible rows found</b>" if candidates else "⚠️ <b>Строка не найдена</b>",
        "",
        "<b>Kommo лид:</b>",
        _esc(snapshot.name),
        _esc(phone_display),
        _esc(snapshot.email),
        _esc(snapshot.product),
    ]
    if reason:
        lines.extend(["", f"Причина: {_esc(_REASON_LABEL_RU.get(reason, reason))}"])
    if candidates:
        lines.extend(["", "<b>Possible matches:</b>"])
        for row in candidates[:8]:
            lines.append(
                f"• row {_esc(row.row_number)} — {_esc(row.client_name)} — {_esc(row.product)}"
            )
    rows: list[list[dict[str, str]]] = [
        [{"text": str(row.row_number), "callback_data": f"lp:pick:{job_id}:{row.row_number}"}]
        for row in candidates[:8]
    ]
    rows.append([{"text": "⏭ Skip", "callback_data": f"lp:skip:{job_id}"}])
    return "\n".join(lines)[:4000], {"inline_keyboard": rows}


def render_error(job: LeadProcessingJob) -> tuple[str, dict[str, Any]]:
    lines = [
        "❌ <b>Ошибка обработки лида</b>",
        "",
        f"Kommo ID: <code>{_esc(job.kommo_lead_id)}</code>",
        f"Чекпоинт: <code>{_esc(job.current_checkpoint)}</code>",
        f"Код ошибки: <code>{_esc(job.error_code)}</code>",
        _esc(job.error_message),
    ]
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🔄 Retry", "callback_data": f"lp:apply:{job.id}"},
                {"text": "🛠 Open manually", "callback_data": f"lp:open:{job.id}"},
                {"text": "⏭ Skip", "callback_data": f"lp:skip:{job.id}"},
            ]
        ]
    }
    return "\n".join(lines)[:4000], keyboard


def render_completed(job: LeadProcessingJob, qualification: LeadQualification | None) -> tuple[str, dict[str, Any]]:
    lines = [
        "✅ <b>Лид обработан</b>",
        "",
        f"Внутренний номер: №{_esc(job.assigned_number)}",
        f"Kommo ID: <code>{_esc(job.kommo_lead_id)}</code>",
    ]
    rows: list[list[dict[str, str]]] = []
    if qualification and qualification.recommended_action == "whatsapp":
        already_sent = bool((job.runtime_state_json or {}).get("whatsapp_message_sent"))
        if not already_sent:
            rows.append(
                [
                    {"text": "📲 Send WhatsApp", "callback_data": f"lp:wa:{job.id}"},
                ]
            )
    if qualification and qualification.recommended_action == "phone_call":
        rows.append(
            [
                {"text": "✅ Call completed", "callback_data": f"lp:call_ok:{job.id}"},
                {"text": "📵 No answer", "callback_data": f"lp:call_na:{job.id}"},
            ]
        )
    rows.append([{"text": "➡️ Следующий лид", "callback_data": "lp:show"}])
    return "\n".join(lines)[:4000], {"inline_keyboard": rows}


def render_whatsapp_share(job: LeadProcessingJob, qualification: LeadQualification, snapshot: LeadSnapshot) -> tuple[str, dict[str, Any]]:
    text = qualification.client_message.text
    link = phone_utils.whatsapp_link(snapshot.phone, text)
    lines = ["📲 <b>Сообщение для WhatsApp</b>", "", _esc(text)]
    rows: list[list[dict[str, str]]] = []
    if link:
        rows.append([{"text": "Открыть в WhatsApp", "url": link}])
    rows.append([{"text": "✅ Message sent", "callback_data": f"lp:wa_sent:{job.id}"}])
    return "\n".join(lines)[:4000], {"inline_keyboard": rows}


def render_call_script(qualification: LeadQualification, job: LeadProcessingJob) -> tuple[str, dict[str, Any]]:
    script = qualification.call_script
    lines = ["📞 <b>Подготовка к звонку</b>", ""]
    if script:
        lines.extend(
            [
                f"<b>Цель:</b> {_esc(script.objective)}",
                "",
                f"<b>Открывающая фраза:</b> {_esc(script.opening_phrase)}",
            ]
        )
        if script.questions:
            lines.extend(["", "<b>Вопросы:</b>"])
            lines.extend(f"{index}. {_esc(item)}" for index, item in enumerate(script.questions, 1))
        if script.possible_objections:
            lines.extend(["", "<b>Возможные возражения:</b>"])
            lines.extend(f"• {_esc(item)}" for item in script.possible_objections)
        if script.recommended_answers:
            lines.extend(["", "<b>Рекомендуемые ответы:</b>"])
            lines.extend(f"• {_esc(item)}" for item in script.recommended_answers)
        lines.extend(["", f"<b>Закрывающая фраза:</b> {_esc(script.closing_phrase)}"])
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Call completed", "callback_data": f"lp:call_ok:{job.id}"},
                {"text": "📵 No answer", "callback_data": f"lp:call_na:{job.id}"},
            ],
            [
                {"text": "📅 Перенести", "callback_data": f"lp:call_later:{job.id}"},
                {"text": "❌ Номер неверный", "callback_data": f"lp:call_bad:{job.id}"},
            ],
        ]
    }
    return "\n".join(lines)[:4000], keyboard


def edit_menu(job: LeadProcessingJob) -> tuple[str, dict[str, Any]]:
    lines = ["✏️ <b>Что изменить?</b>", "", "Отправьте новое значение обычным сообщением после выбора поля."]
    fields = [
        ("Товар (перевод)", "product_name_ru"),
        ("Приоритет", "priority"),
        ("Рекомендуемое действие", "recommended_action"),
        ("Сообщение клиенту", "client_message_text"),
        ("Примечание Kommo", "kommo_note_ru"),
        ("Название задачи", "task_title_ru"),
        ("Срок задачи (ISO)", "task_due_at"),
    ]
    rows = [
        [{"text": label, "callback_data": f"lp:edit_field:{job.id}:{field}"}]
        for label, field in fields
    ]
    rows.append([{"text": "⬅️ Назад", "callback_data": f"lp:show_job:{job.id}"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def no_leads_message() -> str:
    return "✅ Новых лидов Facebook для обработки нет."
