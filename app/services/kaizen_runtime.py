"""Final runtime layer for kaizen reflection and deterministic short commands."""
from __future__ import annotations

import html
import hashlib
import logging
import re
from datetime import date
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.agent import actions, executor, memory, planner, service as agent_service
from app.agent.contracts import AgentPlan, AgentReply
from app.services import identity_service, kaizen_journal_service

logger = logging.getLogger(__name__)
_INSTALLED = False

_DAILY_PHRASES = (
    "подведем итоги дня",
    "подведём итоги дня",
    "хочу рассказать как прошел день",
    "хочу рассказать как прошёл день",
    "запиши итоги дня",
    "как прошел день",
    "как прошёл день",
    "вечерняя рефлексия",
)
_WEEKLY_PHRASES = (
    "подведи итоги недели",
    "подведем итоги недели",
    "подведём итоги недели",
    "что ты понял за эту неделю",
    "что ты понял за неделю",
    "какие проблемы повторялись на неделе",
    "итоги недели",
    "анализ недели",
)
_APPEND_PREFIXES = (
    "дополни дневник",
    "добавь в дневник",
    "еще в дневник",
    "ещё в дневник",
)
_KAIZEN_WORDS = ("kaizen", "кайдзен", "кайзен")


def _normal(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def _is_daily_request(text: str) -> bool:
    normalized = _normal(text)
    return normalized == "/evening" or any(
        _normal(phrase) in normalized for phrase in _DAILY_PHRASES
    )


def _is_weekly_request(text: str) -> bool:
    normalized = _normal(text)
    return normalized == "/week" or any(
        _normal(phrase) in normalized for phrase in _WEEKLY_PHRASES
    )


def _extract_append_text(text: str) -> tuple[bool, str]:
    normalized = _normal(text)
    for phrase in _APPEND_PREFIXES:
        marker = _normal(phrase)
        if normalized.startswith(marker):
            original = str(text or "").strip()
            tail = re.sub(
                rf"^\s*{re.escape(phrase)}\s*[:—-]?\s*",
                "",
                original,
                count=1,
                flags=re.I,
            ).strip()
            return True, tail
    return False, ""


def _extract_kaizen_capture(text: str) -> tuple[bool, str, str]:
    """Recognise an explicit Kaizen destination before Kommo intent routing."""
    raw = str(text or "").strip()
    normalized = _normal(raw)
    if not any(word in normalized for word in _KAIZEN_WORDS):
        return False, "", ""
    if _is_daily_request(raw) or _is_weekly_request(raw):
        return False, "", ""

    body = re.sub(
        r"^\s*(?:добавь|добавить|запиши|записать|создай|поставь|сохрани)?\s*"
        r"(?:в|на)?\s*(?:kaizen|кайдзен|кайзен)\s*[:—-]?\s*",
        "",
        raw,
        count=1,
        flags=re.I,
    ).strip()
    if body == raw:
        body = re.sub(
            r"\s+(?:в|на)\s+(?:kaizen|кайдзен|кайзен)\s*$",
            "",
            raw,
            count=1,
            flags=re.I,
        ).strip()
    if not body or _normal(body) in _KAIZEN_WORDS:
        return True, "", "Запись"

    normal_body = _normal(body)
    if any(token in normal_body for token in ("мысль", "идея", "наблюдение")):
        kind = "Идея / мысль"
    elif any(token in normal_body for token in ("улучш", "оптимиз", "изменить процесс")):
        kind = "Улучшение"
    elif any(
        token in normal_body
        for token in ("план", "задача недели", "задачу недели", "на неделе", "на неделю")
    ):
        kind = "План"
    else:
        kind = "Задача"
    return True, body, kind


def _kaizen_title(body: str, kind: str) -> str:
    clean = re.sub(
        r"^\s*(?:задач[ау]?\s+недели|задач[ау]?|мысль|идея|улучшение|план(?:\s+недели)?|"
        r"отч[её]т(?:\s+недели)?)\s*[:—-]?\s*",
        "",
        body,
        count=1,
        flags=re.I,
    ).strip()
    return (clean or f"Kaizen — {kind}")[:200]


def _active_context_plan(text: str, context: dict[str, Any]) -> AgentPlan | None:
    normalized = _normal(text)
    active_id = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
    if not active_id:
        return None
    if normalized in {
        "покажи текущий проект",
        "открой текущий проект",
        "что по текущему",
        "что по нему",
        "что по ней",
        "покажи его",
        "покажи ее",
        "покажи её",
        "сюда",
    }:
        return AgentPlan(
            intent="project_snapshot",
            mode="read",
            confidence=1.0,
            lead_id=int(active_id),
            rationale="Короткая команда разрешена через активный проект.",
        )
    if normalized in {"история", "история проекта", "хронология", "что уже было"}:
        return AgentPlan(
            intent="project_history",
            mode="read",
            confidence=1.0,
            lead_id=int(active_id),
        )
    return None


def smarter_deterministic_plan(
    original,
    text: str,
    context: dict[str, Any],
) -> AgentPlan | None:
    normalized = _normal(text)
    if _is_daily_request(text):
        return AgentPlan(intent="daily_reflection", mode="read", confidence=1.0)
    if _is_weekly_request(text):
        return AgentPlan(intent="weekly_review", mode="read", confidence=1.0)
    append, body = _extract_append_text(text)
    if append:
        return AgentPlan(
            intent="daily_reflection_append",
            mode="answer",
            confidence=1.0,
            body=body or None,
        )

    exact: dict[str, tuple[str, str]] = {
        "сводка": ("daily_digest", "read"),
        "приоритеты": ("daily_digest", "read"),
        "что горит": ("daily_digest", "read"),
        "что срочно": ("daily_digest", "read"),
        "что сегодня": ("daily_plan", "read"),
        "план дня": ("daily_plan", "read"),
        "на сегодня": ("daily_plan", "read"),
        "просрочка": ("overdue_actions", "read"),
        "что просрочено": ("overdue_actions", "read"),
        "без шага": ("without_next_action", "read"),
        "без задачи": ("without_next_action", "read"),
        "где нет задачи": ("without_next_action", "read"),
        "ждут нас": ("waiting_us", "read"),
        "мы ждем": ("waiting_client", "read"),
        "мы ждём": ("waiting_client", "read"),
        "зависшие": ("stale_projects", "read"),
    }
    if normalized in exact:
        intent, mode = exact[normalized]
        return AgentPlan(intent=intent, mode=mode, confidence=1.0)

    project_match = re.fullmatch(
        r"(?:проект|сделка|лид)?\s*[№#]?\s*(\d{1,4})(?:\s+(?:проект|сделка|лид))?",
        normalized,
    )
    if project_match:
        return AgentPlan(
            intent="project_snapshot",
            mode="read",
            confidence=0.99,
            query=project_match.group(1),
        )

    context_plan = _active_context_plan(text, context)
    if context_plan is not None:
        return context_plan
    return original(text, context)


def _explicit_command_during_reflection(text: str, context: dict[str, Any]) -> bool:
    raw = str(text or "").strip()
    if raw.startswith("/"):
        return True
    if _is_daily_request(raw) or _is_weekly_request(raw):
        return True
    append, _ = _extract_append_text(raw)
    if append:
        return False
    normalized = _normal(raw)
    if normalized in {
        "сводка", "приоритеты", "что горит", "что срочно", "что сегодня",
        "план дня", "на сегодня", "просрочка", "что просрочено", "без шага",
        "без задачи", "где нет задачи", "ждут нас", "мы ждем", "зависшие",
        "история", "хронология",
    }:
        return True
    if re.fullmatch(r"(?:проект|сделка|лид)\s*[№#]?\s*\d{1,12}", normalized):
        return True
    return bool(
        re.match(
            r"^(?:покажи|найди|открой|создай|добавь|поставь|сделай|подготовь|проверь)\b",
            normalized,
        )
        and not normalized.startswith(("запиши итоги", "добавь в дневник"))
    )


async def _remember_reply(
    db: Any,
    *,
    session: Any,
    text: str,
    source: str,
    reply: AgentReply,
) -> AgentReply:
    await memory.remember_message(
        db,
        session=session,
        role="user",
        content=text,
        source=source,
        intent=reply.intent,
    )
    if reply.text:
        await memory.remember_message(
            db,
            session=session,
            role="assistant",
            content=reply.text,
            source="agent",
            intent=reply.intent,
            metadata=reply.metadata,
        )
    return reply


async def _daily_invitation(
    db: Any,
    *,
    telegram_user_id: int,
    session: Any,
    source: str,
) -> AgentReply:
    entry = await kaizen_journal_service.start_daily_reflection(
        db,
        telegram_user_id=telegram_user_id,
        session=session,
        source=source,
    )
    if entry.status == "skipped":
        return AgentReply(
            "Сегодняшняя рефлексия была пропущена. Напиши «Дополни дневник: …», чтобы всё-таки добавить запись.",
            intent="daily_reflection_skipped",
            metadata={"entry_id": int(entry.id)},
        )
    return AgentReply(
        kaizen_journal_service.reflection_invitation_text(),
        reply_markup=kaizen_journal_service.reflection_invitation_markup(entry.period_start),
        intent="daily_reflection",
        metadata={"entry_id": int(entry.id), "local_date": entry.period_start.isoformat()},
    )


async def _save_reflection(
    db: Any,
    *,
    telegram_user_id: int,
    session: Any,
    text: str,
    source: str,
    append: bool,
) -> AgentReply:
    if not kaizen_journal_service.is_meaningful_reflection(text):
        return AgentReply(
            "Расскажи чуть подробнее одним сообщением или голосом: что сегодня получилось и что мешало?",
            intent="daily_reflection_followup",
        )
    entry, analysis_ok = await kaizen_journal_service.save_daily_reflection(
        db,
        telegram_user_id=telegram_user_id,
        session=session,
        text=text,
        source=source,
        append=append,
    )
    return AgentReply(
        kaizen_journal_service.format_daily_entry(entry, analysis_ok=analysis_ok),
        intent="daily_reflection_saved",
        metadata={
            "entry_id": int(entry.id),
            "source": source,
            "analysis_ok": analysis_ok,
            "appended": append,
        },
    )


async def _weekly_review(
    db: Any,
    *,
    telegram_user_id: int,
    force_rebuild: bool = False,
    week_start: date | None = None,
) -> AgentReply:
    entry, analysis_ok = await kaizen_journal_service.build_weekly_review(
        db,
        telegram_user_id=telegram_user_id,
        week_start=week_start,
        force_rebuild=force_rebuild,
    )
    text = kaizen_journal_service.format_weekly_review(entry, analysis_ok=analysis_ok)
    if (
        (entry.analysis or {}).get("improvement_candidates")
        and not kaizen_journal_service.notion_improvements_available()
    ):
        text += (
            "\n\nℹ️ Создание карточек скрыто: база Tasks Notion недоступна. "
            "Проверь подключение командой <code>/notion_test</code>."
        )
    return AgentReply(
        text,
        reply_markup=kaizen_journal_service.weekly_review_markup(entry),
        intent="weekly_review",
        metadata={"entry_id": int(entry.id), "analysis_ok": analysis_ok},
    )


async def _execute_notion_improvements(db: Any, action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    if int(payload.get("telegram_user_id") or 0) != int(action.telegram_user_id):
        raise PermissionError("Kaizen action owner mismatch.")
    entry = await kaizen_journal_service.get_entry_by_id(
        db,
        entry_id=int(payload["weekly_entry_id"]),
        telegram_user_id=int(action.telegram_user_id),
        lock=True,
    )
    if entry is None or entry.entry_type != "weekly" or entry.status != "completed":
        raise ValueError("Недельная запись не найдена или ещё не завершена.")
    if (
        entry.period_start.isoformat() != str(payload.get("week_start"))
        or entry.period_end.isoformat() != str(payload.get("week_end"))
    ):
        raise ValueError("Период недельного анализа изменился. Пересобери preview.")

    current = kaizen_journal_service.normalise_weekly_analysis(
        entry.analysis or {},
        completed_days=int((entry.analysis or {}).get("completed_days") or 0),
    )["improvement_candidates"][:3]
    staged = list(payload.get("items") or [])[:3]
    if [item.get("title") for item in current] != [item.get("title") for item in staged]:
        raise ValueError("Предложения недели изменились после preview.")

    stored_pages = list(entry.notion_page_ids or [])
    stored_by_external = {
        str(item.get("external_id")): item
        for item in stored_pages
        if isinstance(item, dict) and item.get("external_id")
    }
    item_results = dict(payload.get("item_results") or {})
    lines = ["<b>Карточки Kaizen в Notion</b>", ""]
    failures = 0
    success = 0
    for index, item in enumerate(staged, 1):
        external_id = f"kaizen:{entry.id}:{index}"
        prior = item_results.get(external_id) or {}
        if external_id in stored_by_external or prior.get("status") == "ok":
            success += 1
            item_results[external_id] = {
                "status": "ok",
                "result": stored_by_external.get(external_id) or prior.get("result"),
            }
            lines.append(f"✅ {html.escape(str(item.get('title') or index))} — уже создано")
            continue
        try:
            page = await kaizen_journal_service.create_notion_improvement_page(
                weekly_entry=entry,
                item=item,
                index=index,
            )
            page_record = {
                "external_id": external_id,
                "page_id": page.get("id"),
                "url": page.get("url"),
            }
            if external_id not in stored_by_external:
                stored_pages.append(page_record)
                stored_by_external[external_id] = page_record
                entry.notion_page_ids = stored_pages
                flag_modified(entry, "notion_page_ids")
                await db.commit()
            item_results[external_id] = {"status": "ok", "result": page_record}
            success += 1
            lines.append(f"✅ {html.escape(str(item.get('title') or index))}")
        except Exception as exc:
            failures += 1
            item_results[external_id] = {
                "status": "failed",
                "error": str(exc)[:500],
            }
            lines.append(
                f"❌ {html.escape(str(item.get('title') or index))} — "
                f"{html.escape(exc.__class__.__name__)}"
            )
        payload["item_results"] = item_results
        action.payload = payload
        flag_modified(action, "payload")
        await db.commit()

    if failures and not success:
        raise RuntimeError("Notion не создал ни одной карточки улучшения.")
    lines.extend(["", f"Готово: {success} · Ошибок: {failures}"])
    return {
        "text": "\n".join(lines),
        "data": {
            "weekly_entry_id": int(entry.id),
            "item_results": item_results,
            "notion_page_ids": stored_pages,
        },
        "partial_failed": failures > 0,
        "error_message": "Часть карточек Notion не создана." if failures else None,
    }


async def _execute_notion_kaizen_item(db: Any, action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    if int(payload.get("telegram_user_id") or 0) != int(action.telegram_user_id):
        raise PermissionError("Kaizen action owner mismatch.")
    page = await kaizen_journal_service.create_notion_kaizen_item(
        title=str(payload.get("title") or "Kaizen"),
        details=str(payload.get("details") or ""),
        item_kind=str(payload.get("item_kind") or "Задача"),
        external_id=str(payload.get("external_id") or f"kaizen-action:{action.id}"),
    )
    return {
        "text": (
            "✅ Добавлено в Kaizen: "
            + html.escape(str(payload.get("title") or "Запись"))
        ),
        "data": page,
    }


def install_kaizen_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_deterministic = planner.deterministic_plan

    def deterministic_plan_with_kaizen(text: str, context: dict[str, Any]) -> AgentPlan | None:
        return smarter_deterministic_plan(original_deterministic, text, context)

    planner.deterministic_plan = deterministic_plan_with_kaizen

    original_handle_message = agent_service.handle_message

    async def handle_message_with_kaizen(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ) -> AgentReply:
        session = await memory.get_or_create_session(
            db, telegram_user_id=telegram_user_id
        )
        context = await memory.build_context(
            db, telegram_user_id=telegram_user_id, session=session
        )
        if active_kommo_lead_id is not None:
            context["active_kommo_lead_id"] = int(active_kommo_lead_id)

        is_kaizen_capture, capture_body, capture_kind = _extract_kaizen_capture(text)
        if is_kaizen_capture:
            if not capture_body:
                reply = AgentReply(
                    "Что именно добавить в Kaizen: задачу, план, мысль или улучшение?",
                    intent="kaizen_capture_clarify",
                )
                return await _remember_reply(
                    db, session=session, text=text, source=source, reply=reply
                )
            if not kaizen_journal_service.notion_improvements_available():
                reply = AgentReply(
                    "⚠️ База Tasks Notion недоступна. Проверь /notion_test.",
                    intent="kaizen_capture_unavailable",
                )
                return await _remember_reply(
                    db, session=session, text=text, source=source, reply=reply
                )
            title = _kaizen_title(capture_body, capture_kind)
            external_seed = (
                f"{telegram_user_id}:{capture_kind}:{capture_body.casefold()}".encode("utf-8")
            )
            external_id = "kaizen-capture:" + hashlib.sha256(external_seed).hexdigest()[:24]
            preview = (
                "<b>Добавить в Kaizen?</b>\n\n"
                f"Категория: <b>{html.escape(capture_kind)}</b>\n"
                f"Задача: {html.escape(title)}\n"
                f"Описание: {html.escape(capture_body)}\n\n"
                "Notion: Тип=Improvement · Статус=Todo · Источник=Kaizen"
            )
            action = await actions.stage_action(
                db,
                telegram_user_id=telegram_user_id,
                chat_id=int(chat_id or telegram_user_id),
                action_type="create_notion_kaizen_item",
                payload={
                    "telegram_user_id": int(telegram_user_id),
                    "title": title,
                    "details": capture_body,
                    "item_kind": capture_kind,
                    "external_id": external_id,
                },
                preview_text=preview,
            )
            reply = AgentReply(
                preview,
                reply_markup=actions.approval_markup(action.id),
                intent="kaizen_capture_preview",
                metadata={"action_id": int(action.id)},
            )
            return await _remember_reply(
                db, session=session, text=text, source=source, reply=reply
            )

        append, append_text = _extract_append_text(text)
        if append:
            if not append_text:
                await kaizen_journal_service.start_daily_reflection(
                    db,
                    telegram_user_id=telegram_user_id,
                    session=session,
                    source="command",
                )
                reply = AgentReply(
                    "Расскажи одним сообщением или голосом, что нужно добавить к сегодняшней записи.",
                    intent="daily_reflection_append_prompt",
                )
            else:
                reply = await _save_reflection(
                    db,
                    telegram_user_id=telegram_user_id,
                    session=session,
                    text=append_text,
                    source=source,
                    append=True,
                )
            return await _remember_reply(
                db, session=session, text=text, source=source, reply=reply
            )

        if _is_daily_request(text):
            reply = await _daily_invitation(
                db,
                telegram_user_id=telegram_user_id,
                session=session,
                source="command",
            )
            return await _remember_reply(
                db, session=session, text=text, source=source, reply=reply
            )
        if _is_weekly_request(text):
            reply = await _weekly_review(db, telegram_user_id=telegram_user_id)
            return await _remember_reply(
                db, session=session, text=text, source=source, reply=reply
            )

        pending = await kaizen_journal_service.active_pending_reflection(
            db, session=session
        )
        if pending and not _explicit_command_during_reflection(text, context):
            reply = await _save_reflection(
                db,
                telegram_user_id=telegram_user_id,
                session=session,
                text=text,
                source=source,
                append=False,
            )
            return await _remember_reply(
                db, session=session, text=text, source=source, reply=reply
            )

        return await original_handle_message(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            text=text,
            source=source,
            allow_conversation_passthrough=allow_conversation_passthrough,
            active_kommo_lead_id=active_kommo_lead_id,
        )

    agent_service.handle_message = handle_message_with_kaizen

    original_callback = agent_service.handle_callback

    async def handle_callback_with_kaizen(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ) -> AgentReply | None:
        if not callback_data.startswith("agent:kaizen:"):
            return await original_callback(
                db,
                callback_data=callback_data,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
            )
        parts = callback_data.split(":")
        if len(parts) < 4:
            return AgentReply("❌ Некорректная команда дневника.", intent="kaizen_callback_error")
        command = parts[2]
        session = await memory.get_or_create_session(
            db, telegram_user_id=telegram_user_id
        )
        if command in {"start", "later", "skip"}:
            try:
                day = date.fromisoformat(parts[3])
            except ValueError:
                return AgentReply("⌛ Дата рефлексии устарела.", intent="kaizen_callback_stale")
            if day != kaizen_journal_service.local_date():
                return AgentReply("⌛ Этот вечерний запрос уже устарел.", intent="kaizen_callback_stale")
            if command == "start":
                entry = await kaizen_journal_service.start_daily_reflection(
                    db,
                    telegram_user_id=telegram_user_id,
                    session=session,
                    source="command",
                )
                return AgentReply(
                    "🎙 Расскажи одним текстовым или голосовым сообщением, как прошёл день.",
                    intent="daily_reflection_waiting",
                    metadata={"entry_id": int(entry.id)},
                )
            if command == "later":
                await kaizen_journal_service.remind_daily_reflection_later(
                    db,
                    telegram_user_id=telegram_user_id,
                    session=session,
                    day=day,
                )
                return AgentReply(
                    "⏰ Хорошо, напомню через час.",
                    intent="daily_reflection_reminded",
                )
            await kaizen_journal_service.skip_daily_reflection(
                db,
                telegram_user_id=telegram_user_id,
                session=session,
                day=day,
            )
            return AgentReply(
                "⏭ Сегодня пропускаем. Завтра можно начать заново командой /evening.",
                intent="daily_reflection_skipped",
            )

        if command in {"weekcreate", "weekcancel", "weekrebuild"} and parts[3].isdigit():
            entry = await kaizen_journal_service.get_entry_by_id(
                db,
                entry_id=int(parts[3]),
                telegram_user_id=telegram_user_id,
            )
            if entry is None or entry.entry_type != "weekly":
                return AgentReply("⌛ Недельный отчёт не найден.", intent="weekly_review_missing")
            if command == "weekcancel":
                return AgentReply(
                    "✏️ Карточки не созданы. Недельный отчёт сохранён локально.",
                    intent="weekly_improvements_cancelled",
                )
            if command == "weekrebuild":
                return await _weekly_review(
                    db,
                    telegram_user_id=telegram_user_id,
                    force_rebuild=True,
                    week_start=entry.period_start,
                )
            actor = identity_service.current_user()
            if actor is not None and not identity_service.can_write(actor):
                return AgentReply(
                    "🔒 Роль Viewer не позволяет создавать карточки Notion.",
                    intent="permission_denied",
                )
            if entry.notion_page_ids:
                return AgentReply(
                    "ℹ️ Карточки этой недели уже были созданы в Notion.",
                    intent="weekly_improvements_already_created",
                )
            if not kaizen_journal_service.notion_improvements_available():
                return AgentReply(
                    "⚠️ База Tasks Notion недоступна. Проверь /notion_test.",
                    intent="weekly_improvements_unavailable",
                )
            preview, items = kaizen_journal_service.notion_action_preview(entry)
            if not items:
                return AgentReply(
                    "Для этой недели нет подтверждённых кандидатов улучшений.",
                    intent="weekly_improvements_empty",
                )
            action = await actions.stage_action(
                db,
                telegram_user_id=telegram_user_id,
                chat_id=int(chat_id or telegram_user_id),
                action_type="create_notion_improvements_batch",
                payload={
                    "telegram_user_id": int(telegram_user_id),
                    "weekly_entry_id": int(entry.id),
                    "week_start": entry.period_start.isoformat(),
                    "week_end": entry.period_end.isoformat(),
                    "items": items[:3],
                    "item_results": {},
                },
                preview_text=preview,
            )
            return AgentReply(
                preview,
                reply_markup=actions.approval_markup(action.id),
                intent="weekly_improvements_preview",
                metadata={"action_id": int(action.id), "entry_id": int(entry.id)},
            )
        return AgentReply("❌ Некорректная команда дневника.", intent="kaizen_callback_error")

    agent_service.handle_callback = handle_callback_with_kaizen

    original_execute = executor._execute

    async def execute_with_kaizen(db: Any, action: Any) -> dict[str, Any]:
        if action.action_type == "create_notion_improvements_batch":
            return await _execute_notion_improvements(db, action)
        if action.action_type == "create_notion_kaizen_item":
            return await _execute_notion_kaizen_item(db, action)
        return await original_execute(db, action)

    executor._execute = execute_with_kaizen
    logger.info("Kaizen journal and smarter command runtime installed")
