"""Compatibility patches for round-three callbacks and artifact precedence."""
from __future__ import annotations

from typing import Any

from app.services import project_artifact_service, round3_runtime

# This module is imported before install_round3_runtime() is called, so this is the
# unwrapped classifier with the full explicit catalog/invoice/certificate rules.
_BASE_CLASSIFIER = project_artifact_service.classify_artifact
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

    def classify_artifact_with_precedence(
        *,
        filename: str,
        mime_type: str,
        caption: str | None,
        kind: str | None = None,
    ):
        base = _BASE_CLASSIFIER(
            filename=filename,
            mime_type=mime_type,
            caption=caption,
            kind=kind,
        )
        caption_text = round3_runtime._clean(caption).casefold()
        supplier_hint = any(
            token in caption_text
            for token in (
                "фабрик",
                "производител",
                "поставщик",
                "supplier",
                "factory",
                "manufacturer",
            )
        )
        # Explicit semantic types always win. A generic document/spreadsheet plus
        # "фабрика" means a supplier offer; "каталог фабрики" remains a catalog.
        if supplier_hint and base.artifact_type in {"document", "spreadsheet"}:
            return project_artifact_service.ArtifactClassification(
                "supplier_offer",
                "Предложение производителя",
                "04 Прайсы фабрик",
                "caption_supplier_hint",
                0.99,
            )
        return base

    round3_runtime._comment_preview_markup = comment_preview_markup
    project_artifact_service.classify_artifact = classify_artifact_with_precedence
