"""Lightweight conversation analysis with explicit uncertainty handling."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConversationAnalysis:
    project: str | None = None
    language: str = "ru"
    participants: list[str] = field(default_factory=list)
    summary: str = ""
    facts: list[str] = field(default_factory=list)
    client_requests: list[str] = field(default_factory=list)
    client_objections: list[str] = field(default_factory=list)
    promises_by_us: list[str] = field(default_factory=list)
    promises_by_client: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    unanswered_questions: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    sentiment: str = "neutral"
    next_actions: list[str] = field(default_factory=list)
    suggested_task: str | None = None
    suggested_followup: str | None = None
    confidence: float = 0.4
    uncertain: list[str] = field(default_factory=list)


def analyze_conversation_text(text: str, *, project: str | None = None) -> ConversationAnalysis:
    raw = str(text or "").strip()
    result = ConversationAnalysis(project=project, summary=raw[:500])
    if len(raw) < 20:
        result.uncertain.append("Не могу достоверно определить детали: слишком мало текста")
        result.missing_information.append("полный текст переговоров")
        result.confidence = 0.1
        return result

    lower = raw.casefold()
    if any(token in lower for token in ("обеща", "пришлём", "пришлем", "вышлем", "отправим")):
        result.promises_by_us.append("Есть формулировка обещания с нашей стороны — уточните детали")
    if any(token in lower for token in ("клиент сказал", "они пришлют", "пришлёт", "пришлет")):
        result.promises_by_client.append("Клиент что-то обещал — проверьте формулировку")
    if "?" in raw or "уточн" in lower:
        result.unanswered_questions.append("В тексте есть вопросы — проверьте, на все ли дан ответ")
    if any(token in lower for token in ("дорог", "дорого", "дороговат", "дешев")):
        result.client_objections.append("Возможное возражение по цене")
        result.sentiment = "cautious"
    if any(token in lower for token in ("нужн", "хотят", "интересу")):
        result.client_requests.append("Есть запрос клиента — зафиксируйте требования отдельно")

    # Unusual / digital-vs-physical ambiguity
    if any(token in lower for token in ("saas", "лиценз", "подписк", "software", "app", "сайт")):
        result.missing_information.append("это физический или цифровой продукт?")
        result.uncertain.append("Не могу достоверно определить тип продукта без уточнения")
    if not any(token in lower for token in ("шт", "моq", "фоb", "cif", "кг", "размер")):
        if "товар" in lower or "стан" in lower:
            result.missing_information.extend(["объём", "техническое описание"])

    result.facts = [f"Длина текста: {len(raw)} символов"]
    result.next_actions = ["Зафиксировать следующий шаг и срок"]
    result.suggested_task = "Уточнить открытые вопросы и ответить клиенту"
    result.suggested_followup = "Короткий follow-up с уточнением недостающих данных"
    result.confidence = 0.55 if result.promises_by_us or result.client_requests else 0.35
    if result.uncertain:
        result.confidence = min(result.confidence, 0.4)
    return result


def format_conversation_analysis(result: ConversationAnalysis) -> str:
    lines = ["<b>🧠 Анализ переговоров</b>", ""]
    if result.uncertain:
        lines.extend(f"⚠️ {html.escape(item)}" for item in result.uncertain)
        lines.append("")
    lines.append(html.escape(result.summary[:800]))
    if result.facts:
        lines.extend(["", "<b>Факты</b>"])
        lines.extend(f"• {html.escape(x)}" for x in result.facts[:6])
    if result.client_requests:
        lines.extend(["", "<b>Запросы клиента</b>"])
        lines.extend(f"• {html.escape(x)}" for x in result.client_requests[:6])
    if result.promises_by_us:
        lines.extend(["", "<b>Наши обещания</b>"])
        lines.extend(f"• {html.escape(x)}" for x in result.promises_by_us[:6])
    if result.missing_information:
        lines.extend(["", "<b>Не хватает</b>"])
        lines.extend(f"• {html.escape(x)}" for x in result.missing_information[:8])
    if result.next_actions:
        lines.extend(["", "<b>Дальше</b>"])
        lines.extend(f"• {html.escape(x)}" for x in result.next_actions[:5])
    lines.append("")
    lines.append(f"Уверенность: {result.confidence:.0%}")
    lines.append("<i>Предположения отделены от фактов. Стадия Kommo не меняется.</i>")
    return "\n".join(lines)
