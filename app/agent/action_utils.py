from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def action_key(*, telegram_user_id: int, action_type: str, payload: dict[str, Any]) -> str:
    # Stable across webhook retries. stage_action() already mints a fresh key
    # after executed/rejected/failed/expired rows so legitimate repeats still work.
    raw = f"{telegram_user_id}:{action_type}:{stable_json(payload)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def approval_markup(action_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Подтвердить", "callback_data": f"agent:ok:{action_id}"},
                {"text": "❌ Отменить", "callback_data": f"agent:no:{action_id}"},
            ]
        ]
    }
