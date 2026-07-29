"""Deterministic A/B/C lead assessment (rules-first; AI optional later)."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_v5 import LeadAssessment
from app.services.contact_resolver import resolve_contact


@dataclass
class LeadAssessmentResult:
    grade: str
    score: int
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    confidence: float = 0.7
    recommended_action: str | None = None


def assess_lead(lead: dict[str, Any], *, next_action: str | None = None) -> LeadAssessmentResult:
    score = 40
    reasons: list[str] = []
    risks: list[str] = []
    missing: list[str] = []

    price = lead.get("price")
    try:
        budget = float(price or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if budget >= 20000:
        score += 25
        reasons.append(f"бюджет выше $20 000 ({int(budget)})")
    elif budget >= 5000:
        score += 12
        reasons.append(f"бюджет ${int(budget)}")
    elif budget > 0:
        score += 4
        reasons.append(f"бюджет указан (${int(budget)})")
    else:
        missing.append("бюджет")

    contact = resolve_contact(lead)
    if contact.phone_normalized:
        score += 10
        reasons.append("есть телефон связанного контакта")
    else:
        missing.append("телефон")
        risks.append("нет телефона для follow-up")
    if contact.email:
        score += 5
        reasons.append("есть email")
    if contact.name and " " in (contact.name or ""):
        score += 3
        reasons.append("есть полное имя контакта")

    name = str(lead.get("name") or "")
    if any(token in name.casefold() for token in ("стан", "машин", "лини", "мод", "тех")):
        score += 8
        reasons.append("конкретный товарный запрос в названии")

    notes = lead.get("notes") or []
    if notes:
        score += 6
        reasons.append("есть история примечаний")
    else:
        risks.append("мало истории общения")

    if next_action or lead.get("closest_task_at"):
        score += 8
        reasons.append("есть следующий шаг / задача")
    else:
        risks.append("нет следующего шага")
        score -= 8

    closest = lead.get("closest_task_at")
    from datetime import datetime, timezone

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if isinstance(closest, (int, float)) and int(closest) < now_ts:
        risks.append("есть просроченная задача")
        score -= 10

    score = max(0, min(100, score))
    if score >= 70:
        grade = "A"
    elif score >= 40:
        grade = "B"
    else:
        grade = "C"

    recommended = "запросить недостающие данные и зафиксировать следующий шаг"
    if grade == "A":
        recommended = "приоритетный follow-up и подготовка коммерческого предложения"
    elif grade == "C":
        recommended = "уточнить целевой ли интерес или закрыть как нецелевой"

    return LeadAssessmentResult(
        grade=grade,
        score=score,
        reasons=reasons[:8],
        risks=risks[:6],
        missing_data=missing[:6],
        confidence=0.75,
        recommended_action=recommended,
    )


async def save_assessment(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
    result: LeadAssessmentResult,
) -> LeadAssessment:
    row = LeadAssessment(
        kommo_lead_id=int(kommo_lead_id),
        grade=result.grade,
        score=int(result.score),
        reasons_json=result.reasons,
        risks_json=result.risks,
        missing_data_json=result.missing_data,
        confidence=Decimal(str(result.confidence)),
        recommended_action=result.recommended_action,
        source="rules",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def format_assessment(result: LeadAssessmentResult, *, title: str | None = None) -> str:
    badge = {"A": "🟢 A-лид — высокий потенциал", "B": "🟡 B-лид — нормальный потенциал", "C": "🔴 C-лид — слабый / нецелевой"}
    lines = [f"<b>{badge.get(result.grade, result.grade)}</b>"]
    if title:
        lines.append(html.escape(title))
    lines.extend(["", "<b>Почему:</b>"])
    lines.extend(f"• {html.escape(item)}" for item in (result.reasons or ["недостаточно данных"]))
    if result.risks:
        lines.extend(["", "<b>Риск:</b>"])
        lines.extend(f"• {html.escape(item)}" for item in result.risks)
    if result.missing_data:
        lines.extend(["", "<b>Не хватает:</b>"])
        lines.extend(f"• {html.escape(item)}" for item in result.missing_data)
    lines.extend(["", "<b>Следующий шаг:</b>", html.escape(str(result.recommended_action or "—"))])
    lines.append("")
    lines.append("<i>Оценка не меняет стадию Kommo автоматически.</i>")
    return "\n".join(lines)
