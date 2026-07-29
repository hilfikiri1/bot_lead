"""Pure ranking rules for the daily Kommo digest."""
from __future__ import annotations

from typing import Any


def rank_lead(lead: dict[str, Any], now_ts: int) -> dict[str, Any]:
    closest = int(lead.get("closest_task_at") or 0)
    updated = int(lead.get("updated_at") or 0)
    age_days = max(0, (now_ts - updated) // 86400) if updated else 999

    if closest and closest < now_ts:
        overdue_hours = max(1, (now_ts - closest) // 3600)
        return {
            "score": 1000 + min(overdue_hours, 500),
            "priority": "Высокий",
            "task_type": "Звонок",
            "next_step": "Закрыть просроченную задачу и зафиксировать результат",
            "reason": f"задача просрочена примерно на {overdue_hours} ч.",
        }
    if not closest:
        return {
            "score": 800 + min(age_days, 100),
            "priority": "Высокий",
            "task_type": "Сообщение",
            "next_step": "Назначить конкретный следующий контакт",
            "reason": "в Kommo нет следующей задачи",
        }
    if age_days >= 7:
        return {
            "score": 600 + min(age_days, 100),
            "priority": "Средний",
            "task_type": "Сообщение",
            "next_step": "Проверить статус и сделать follow-up",
            "reason": f"нет обновления около {age_days} дн.",
        }
    return {
        "score": 100 - age_days,
        "priority": "Низкий",
        "task_type": "Другое",
        "next_step": "Проверить актуальность следующего шага",
        "reason": "сделка активна, критических просрочек не найдено",
    }
