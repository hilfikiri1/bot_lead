"""Small compatibility patch for the round-three comment-sync callback shape."""
from __future__ import annotations

from typing import Any

from app.services import round3_runtime

_INSTALLED = False


def install_round3_compat() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    def comment_preview_markup(report: dict[str, Any]) -> dict[str, Any] | None:
        count = int(report.get("updates_count") or 0)
        if not count:
            return None
        query = str(report.get("project_query") or "all")
        digest = str(report.get("digest") or "")
        # The reserved field keeps callback parsing stable and leaves room for a
        # future sync mode without breaking existing Telegram buttons.
        callback = f"agent:comment_sync:confirm:v1:{digest}:{count}:{query}"
        return {
            "inline_keyboard": [
                [
                    {"text": f"✅ Обновить X ({count})", "callback_data": callback},
                    {"text": "❌ Отмена", "callback_data": "agent:comment_sync:cancel"},
                ]
            ]
        }

    round3_runtime._comment_preview_markup = comment_preview_markup
