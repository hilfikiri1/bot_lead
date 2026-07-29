"""Minimal AI usage tracking for Agent v4."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_usage_event import AIUsageEvent

settings = get_settings()

# USD per 1M tokens — update when pricing changes
_MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
    "gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
}


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> Decimal | None:
    pricing = _MODEL_PRICING.get(model) or _MODEL_PRICING.get("gpt-4o")
    if not pricing:
        return None
    cost = (
        Decimal(input_tokens) * pricing["input"] / Decimal(1_000_000)
        + Decimal(output_tokens) * pricing["output"] / Decimal(1_000_000)
    )
    return cost.quantize(Decimal("0.000001"))


async def record_usage(
    db: AsyncSession,
    *,
    operation: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    audio_minutes: float | None = None,
    telegram_user_id: int | None = None,
    kommo_lead_id: int | None = None,
    internal_lead_number: str | None = None,
) -> AIUsageEvent | None:
    estimated = None
    if model and input_tokens is not None and output_tokens is not None:
        estimated = estimate_cost_usd(
            model=model, input_tokens=input_tokens, output_tokens=output_tokens
        )
    event = AIUsageEvent(
        operation=operation[:100],
        model=(model or "")[:100] or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        audio_minutes=Decimal(str(audio_minutes)) if audio_minutes is not None else None,
        estimated_cost_usd=estimated,
        telegram_user_id=telegram_user_id,
        kommo_lead_id=kommo_lead_id,
        internal_lead_number=internal_lead_number,
    )
    try:
        db.add(event)
        await db.commit()
        await db.refresh(event)
        return event
    except Exception:
        await db.rollback()
        return None


async def usage_summary(db: AsyncSession) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)

    async def _sum(since: datetime) -> Decimal:
        result = await db.execute(
            select(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0)).where(
                AIUsageEvent.created_at >= since
            )
        )
        return Decimal(str(result.scalar_one() or 0))

    today_cost = await _sum(today_start)
    month_cost = await _sum(month_start)

    top_ops = await db.execute(
        select(
            AIUsageEvent.operation,
            func.count(AIUsageEvent.id),
            func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0),
        )
        .where(AIUsageEvent.created_at >= month_start)
        .group_by(AIUsageEvent.operation)
        .order_by(func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).desc())
        .limit(5)
    )
    operations = [
        {"operation": row[0], "count": int(row[1]), "cost_usd": float(row[2] or 0)}
        for row in top_ops.all()
    ]
    warnings: list[str] = []
    daily_budget = Decimal(str(settings.agent_daily_ai_budget_usd or 0))
    monthly_budget = Decimal(str(settings.agent_monthly_ai_budget_usd or 0))
    threshold = Decimal(str(settings.agent_cost_warning_percent or 80)) / Decimal(100)
    if daily_budget > 0 and today_cost >= daily_budget * threshold:
        warnings.append(f"Дневной бюджет AI использован на {int(threshold * 100)}%+")
    if monthly_budget > 0 and month_cost >= monthly_budget * threshold:
        warnings.append(f"Месячный бюджет AI использован на {int(threshold * 100)}%+")

    return {
        "today_cost_usd": float(today_cost),
        "month_cost_usd": float(month_cost),
        "top_operations": operations,
        "warnings": warnings,
        "budget_blocking": False,
    }


def format_costs_report(summary: dict[str, Any]) -> str:
    lines = [
        "<b>💰 AI usage</b>",
        "",
        f"Сегодня: <b>${summary.get('today_cost_usd', 0):.4f}</b> (оценка)",
        f"Месяц: <b>${summary.get('month_cost_usd', 0):.4f}</b> (оценка)",
        "",
        "<b>Топ операций (месяц)</b>",
    ]
    for item in summary.get("top_operations") or []:
        lines.append(
            f"• {item['operation']}: {item['count']} × ${item['cost_usd']:.4f}"
        )
    for warning in summary.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    lines.append("")
    lines.append("<i>Read-only. Бюджет 0 = без блокировки.</i>")
    return "\n".join(lines)
