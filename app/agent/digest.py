from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent.lead_refs import enrich_lead, extract_internal_lead_number
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
        enriched = enrich_lead(lead)
        ranking = rank_lead(enriched, now_ts=now_ts)
        ranked.append({"lead": enriched, **ranking})
    ranked.sort(
        key=lambda item: (
            -int(item["score"]),
            -int((item.get("lead") or {}).get("updated_at") or 0),
        )
    )
    max_items = max(1, min(limit or settings.agent_digest_max_items, 20))
    items = ranked[:max_items]
    digest_items = []
    for index, item in enumerate(items, 1):
        lead = item.get("lead") or {}
        internal = extract_internal_lead_number(lead) or lead.get("internal_lead_number")
        digest_items.append(
            {
                "position": index,
                "internal_lead_number": internal,
                "kommo_lead_id": int(lead.get("id") or 0) or None,
                "name": lead.get("name"),
                "url": lead.get("url"),
                "priority": item.get("priority"),
                "reason": item.get("reason"),
                "next_step": item.get("next_step"),
                "score": item.get("score"),
            }
        )
    now = datetime.now(timezone.utc)
    return {
        "items": items,
        "digest_map": digest_items,
        "open_count": len(leads),
        "truncated": bool(result.get("truncated")),
        "scanned_count": result.get("scanned_count"),
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=settings.agent_action_ttl_minutes)).isoformat(),
    }


def format_digest(result: dict[str, Any]) -> str:
    lines = [
        "<b>🧠 Приоритеты на сегодня</b>",
        "",
        f"Открытых сделок: <b>{int(result.get('open_count') or 0)}</b>",
        "",
    ]
    digest_map = result.get("digest_map") or []
    if not digest_map:
        lines.append("Активных сделок не найдено.")
    for item in digest_map:
        position = int(item.get("position") or 0)
        internal = item.get("internal_lead_number")
        name = html.escape(str(item.get("name") or f"Сделка {item.get('kommo_lead_id')}"))
        kommo_id = html.escape(str(item.get("kommo_lead_id") or "—"))
        url = html.escape(str(item.get("url") or ""), quote=True)
        if internal:
            lines.append(f"<b>{position}. №{html.escape(str(internal))} — {name}</b>")
        else:
            lines.append(f"<b>{position}. {name}</b>")
            lines.append("Внутренний номер: не указан")
        lines.extend(
            [
                f"Kommo ID: <code>{kommo_id}</code>",
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


def digest_markup(digest_map: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    for item in digest_map[:8]:
        position = int(item.get("position") or 0)
        kommo_id = item.get("kommo_lead_id")
        if not kommo_id:
            continue
        internal = item.get("internal_lead_number")
        name = str(item.get("name") or kommo_id)
        short_name = name[:28] + ("…" if len(name) > 28 else "")
        if internal:
            label = f"Выбрать №{internal}"
        else:
            label = f"{position}. {short_name}"
        rows.append(
            [
                {
                    "text": label[:64],
                    "callback_data": f"agent:digest:{position}",
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else None


def build_last_digest_context(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": result.get("generated_at"),
        "expires_at": result.get("expires_at"),
        "items": list(result.get("digest_map") or []),
    }
