"""Conservative deterministic aliases for concise manager commands.

The patch never guesses a lead. Contextual writes are emitted only when an active
Kommo lead is already present in AgentSession context; all writes continue through
the existing preview/confirmation flow.
"""
from __future__ import annotations

import re
from typing import Any

from app.agent.contracts import AgentPlan
from app.services import kaizen_runtime

_INSTALLED = False


def _normal(value: str) -> str:
    text = " ".join(str(value or "").strip().casefold().replace("ё", "е").split())
    return text.strip(" \t\r\n?!.,;")


def _active_id(context: dict[str, Any]) -> int | None:
    value = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _language(text: str) -> str:
    normalized = _normal(text)
    if any(token in normalized for token in ("по-польски", "на польском", "po polsku")):
        return "pl"
    if any(token in normalized for token in ("по-украински", "на украинском", "украинською")):
        return "uk"
    if any(token in normalized for token in ("по-английски", "на английском", "in english")):
        return "en"
    if any(token in normalized for token in ("по-китайски", "на китайском", "中文")):
        return "zh"
    if any(token in normalized for token in ("по-немецки", "на немецком", "auf deutsch")):
        return "de"
    return "auto"


def install_context_intelligence_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_daily = kaizen_runtime._is_daily_request
    original_weekly = kaizen_runtime._is_weekly_request
    original_smarter = kaizen_runtime.smarter_deterministic_plan

    daily_aliases = (
        "итоги дня",
        "расскажу про день",
        "рассказать про день",
        "запиши мой день",
        "разберем день",
        "дневник за сегодня",
        "рефлексия дня",
    )
    weekly_aliases = (
        "разбери неделю",
        "как прошла неделя",
        "недельный отчет",
        "недельный отчёт",
        "выводы недели",
        "повторяющиеся проблемы недели",
    )

    def is_daily_request(text: str) -> bool:
        normalized = _normal(text)
        return original_daily(text) or any(alias in normalized for alias in daily_aliases)

    def is_weekly_request(text: str) -> bool:
        normalized = _normal(text)
        return original_weekly(text) or any(alias in normalized for alias in weekly_aliases)

    def smarter(
        original,
        text: str,
        context: dict[str, Any],
    ) -> AgentPlan | None:
        normalized = _normal(text)
        active = _active_id(context)

        read_aliases: dict[str, str] = {
            "горящие": "daily_digest",
            "горящие задачи": "daily_digest",
            "срочное": "daily_digest",
            "кому ответить": "daily_digest",
            "кто ждет нас": "waiting_us",
            "кто ждет": "waiting_us",
            "кого ждем": "waiting_client",
            "без движения": "stale_projects",
            "давно молчат": "stale_projects",
            "нет следующего шага": "without_next_action",
            "где нет шага": "without_next_action",
        }
        if normalized in read_aliases:
            return AgentPlan(
                intent=read_aliases[normalized],
                mode="read",
                confidence=1.0,
                rationale="Короткая операционная команда распознана детерминированно.",
            )

        number = re.fullmatch(
            r"(?:открой|покажи|найди)?\s*[№#]?\s*(\d{1,4})\s*",
            normalized,
        )
        if number:
            return AgentPlan(
                intent="project_snapshot",
                mode="read",
                confidence=0.99,
                query=number.group(1),
            )

        if active:
            note_match = re.match(
                r"^(?:запиши|заметка|в коммо)\s*[:—-]\s*(.+)$",
                str(text or "").strip(),
                flags=re.I,
            )
            if note_match and note_match.group(1).strip():
                return AgentPlan(
                    intent="add_kommo_note",
                    mode="write",
                    confidence=0.98,
                    lead_id=active,
                    note_text=note_match.group(1).strip(),
                    rationale="Короткая заметка относится к активному проекту.",
                )

            if re.match(
                r"^(?:перезвонить|позвонить|связаться|напомнить|уточнить|проверить)\b",
                normalized,
            ) and any(
                token in normalized
                for token in (
                    "сегодня", "завтра", "послезавтра", "понедельник", "вторник",
                    "среду", "среда", "четверг", "пятницу", "пятница", "субботу",
                    "воскресенье", "через", ":",
                )
            ):
                return AgentPlan(
                    intent="create_kommo_task",
                    mode="write",
                    confidence=0.94,
                    lead_id=active,
                    title=str(text).strip(),
                    due_at=str(text).strip(),
                    rationale="Короткая задача относится к активному проекту.",
                )

            if any(
                phrase in normalized
                for phrase in (
                    "ответить ему",
                    "ответить ей",
                    "напиши ему",
                    "напиши ей",
                    "сообщение ему",
                    "сообщение ей",
                    "follow-up ему",
                    "follow up ему",
                )
            ):
                return AgentPlan(
                    intent="generate_draft",
                    mode="draft",
                    confidence=0.95,
                    lead_id=active,
                    draft_kind="followup_message",
                    language=_language(text),
                    rationale="Получатель разрешён через активный проект.",
                )

            if normalized in {
                "открой его",
                "открой ее",
                "открой её",
                "покажи детали",
                "карточка",
                "карточка проекта",
                "что дальше",
                "следующий шаг",
            }:
                return AgentPlan(
                    intent="project_snapshot",
                    mode="read",
                    confidence=0.98,
                    lead_id=active,
                )

        return original_smarter(original, text, context)

    kaizen_runtime._is_daily_request = is_daily_request
    kaizen_runtime._is_weekly_request = is_weekly_request
    kaizen_runtime.smarter_deterministic_plan = smarter
