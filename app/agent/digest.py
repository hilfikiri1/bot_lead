from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.services import kommo_service

settings = get_settings()


def rank_lead(lead: dict[str, Any], *, now_ts: int) -> dict[str, Any]:
    closest_task = lead.get("closest_task_at")
    updated_at = int(lead.get("updated_at") or lead.get("created_at") or 0)
    age_days = max(0, (now_ts - updated_at) // 86_400) if updated_at else 999

    if isinstance(closest_task, (int, float)) and int(closest_task) < now_ts:
        overdue_days = max(0, (now_ts - int(closest_task)) // 86_400)
        return {
            "score": 100 + min(overdue_days, 30),
            "priority": "Высокий",
            "reason": f"просрочена задача на {overdue_days or 1} дн.",
            "next_step": "Связаться с клиентом и закрыть или перенести задачу",
        }
    if closest_task is None:
        return {
            "score": 90 + min(age_days, 20),
            "priority": "Высокий",
            "reason": "нет следующей задачи",
            "next_step": "Определить конкретное следующее касание",
        }
    if age_days >= 14:
        return {
            "score": 75 + min(age_days, 30),
            "priority": "Высокий",
            "reason": f"нет обновлений {age_days} дн.",
            "next_step": "Проверить статус и отправить follow-up",
        }
    if age_days >= 7:
        return {
            "score": 55 + age_days,
            "priority": "Средний",
            "reason": f"нет обновлений {age_days} дн.",
            "next_step": "Подготовить follow-up или уточнить решение клиента",
        }
    return {
        "score": 20 + age_days,
        "priority": "Низкий",
        "reason": "сделка недавно обновлялась",
        "next_step": "Следовать текущему плану",
    }


async def build_digest(*, limit: int | None = None) -> dict[str, Any]:
    result = await kommo_service.get_all_open_leads()
    leads = result.get("leads") or []
    now_ts = int(datetime.now(timezone.utc).timestamp())
    ranked = []
    for lead in leads:
        ranking = rank_lead(lead, now_ts=now_ts)
        ranked.append({"lead": lead, **ranking})
    ranked.sort(
        key=lambda item: (
            -int(item["score"]),
            -int((item.get("lead") or {}).get("updated_at") or 0),
        )
    )
    max_items = max(1, min(limit or settings.agent_digest_max_items, 20))
    return {
        "items": ranked[:max_items],
        "open_count": len(leads),
        "truncated": bool(result.get("truncated")),
        "scanned_count": result.get("scanned_count"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def format_digest(result: dict[str, Any]) -> str:
    lines = [
        "<b>🧠 Приоритеты на сегодня</b>",
        "",
        f"Открытых сделок: <b>{int(result.get('open_count') or 0)}</b>",
        "",
    ]
    items = result.get("items") or []
    if not items:
        lines.append("Активных сделок не найдено.")
    for index, item in enumerate(items, 1):
        lead = item.get("lead") or {}
        name = html.escape(str(lead.get("name") or f"Сделка {lead.get('id')}"))
        url = html.escape(str(lead.get("url") or ""), quote=True)
        lines.extend(
            [
                f"<b>{index}. {name}</b>",
                f"Приоритет: <b>{html.escape(str(item.get('priority') or '—'))}</b>",
                f"Причина: {html.escape(str(item.get('reason') or '—'))}",
                f"Следующий шаг: {html.escape(str(item.get('next_step') or '—'))}",
            ]
        )
        if url:
            lines.append(f'<a href="{url}">Открыть в Kommo</a>')
        lines.append("")
    if result.get("truncated"):
        lines.append("⚠️ Список ограничен лимитом страниц Kommo.")
    lines.append("Дайджест ничего не изменяет во внешних сервисах.")
    return "\n".join(lines)
