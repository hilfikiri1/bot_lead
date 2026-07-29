from __future__ import annotations

import inspect

import pytest

from app.services import (
    comment_sync_service,
    round3_compat,
    round3_runtime,
)
from app.services.google_sheets_service import SpreadsheetRow


def test_latest_meaningful_comment_skips_technical_notes_and_supports_iso_dates() -> None:
    notes = [
        {
            "created_at": "2026-07-29T21:46:00+00:00",
            "text": "[BBS-FILE-4]\nФайл проекта загружен через B&BS Telegram Agent.",
        },
        {
            "created_at": "2026-07-29T20:30:00+00:00",
            "text": (
                "Примечание добавлено через B&BS AI Agent. "
                "Поговорил с клиентом. Готовим цену на все размеры."
            ),
        },
        {
            "created_at": 1785350000,
            "text": "Старая заметка",
        },
    ]

    result = comment_sync_service._latest_meaningful_comment(notes)

    assert result is not None
    assert "Готовим цену на все размеры" in result
    assert "Файл проекта" not in result
    assert result.startswith("29.07:")


def test_comment_digest_ignores_generation_time_but_tracks_real_change() -> None:
    base = {
        "generated_at": "first",
        "updates": [
            {
                "row_number": 135,
                "lead_number": "135",
                "kommo_lead_id": 12496715,
                "old_comment": "старое",
                "new_comment": "новое",
            }
        ],
    }
    same = {**base, "generated_at": "second"}
    changed = {
        **base,
        "updates": [{**base["updates"][0], "new_comment": "другое"}],
    }

    assert comment_sync_service._digest(base) == comment_sync_service._digest(same)
    assert comment_sync_service._digest(base) != comment_sync_service._digest(changed)


@pytest.mark.asyncio
async def test_comment_sync_builds_x_only_update_for_project_135(monkeypatch) -> None:
    row = SpreadsheetRow(
        row_number=135,
        phone="+48 607 783 552",
        email="acukrowski@o2.pl",
        client_name="Artur Cukrowski",
        company=None,
        product="Кормушки",
        lead_number="135",
        lead_status="Получено ТЗ",
        marketing_comment="старый комментарий",
    )

    monkeypatch.setattr(
        comment_sync_service.google_sheets_service,
        "get_rows",
        lambda **kwargs: [row],
    )

    async def fake_leads():
        return {
            "pipeline_name": "Польша (1 этап)",
            "leads": [{"id": 12496715, "name": "135 - Кормушки"}],
        }

    async def fake_notes(lead_id: int, *, limit: int = 30):
        assert lead_id == 12496715
        return [
            {
                "created_at": "2026-07-29T16:29:00+00:00",
                "text": "Готовим цену на все размеры. Ждём количество штук в 1 м³.",
            }
        ]

    monkeypatch.setattr(
        comment_sync_service.kommo_service,
        "get_all_leads_for_status_sync",
        fake_leads,
    )
    monkeypatch.setattr(
        comment_sync_service.kommo_service,
        "get_recent_common_notes",
        fake_notes,
    )

    report = await comment_sync_service.build_comment_sync_report("135")

    assert report["updates_count"] == 1
    update = report["updates"][0]
    assert update["row_number"] == 135
    assert update["old_lead_number"] == "135"
    assert update["new_lead_number"] == "135"
    assert "Ждём количество" in update["new_comment"]
    assert report["column"] == "X"
    assert report["preserves_columns"] == ["W", "Y"]


@pytest.mark.asyncio
async def test_comment_sync_rechecks_digest_before_write(monkeypatch) -> None:
    report = {
        "digest": "new-digest",
        "updates_count": 1,
        "updates": [{"row_number": 135}],
    }

    async def fake_report(project_query=None):
        return report

    called = False

    def fake_apply(updates):
        nonlocal called
        called = True
        return {"updated_count": 1}

    monkeypatch.setattr(comment_sync_service, "build_comment_sync_report", fake_report)
    monkeypatch.setattr(
        comment_sync_service.google_sheets_service,
        "apply_lead_registry_updates",
        fake_apply,
    )

    result = await comment_sync_service.apply_confirmed_report(
        expected_digest="old-digest",
        expected_count=1,
        project_query="135",
    )

    assert result["stale"] is True
    assert called is False


def test_round3_uses_numbered_country_folder_and_alias() -> None:
    assert round3_runtime._COUNTRY_NAMES["PL"] == "01 Польша"
    assert round3_runtime._country_aliases("01 Польша") == ("01 Польша", "Польша")


def test_comment_sync_command_and_callback_fit_telegram_limit() -> None:
    assert round3_runtime._COMMENT_RE.match("/comment_sync")
    match = round3_runtime._COMMENT_RE.match("/comment_sync 135")
    assert match and match.group(1) == "135"

    original = round3_runtime._comment_preview_markup
    original_installed = round3_compat._INSTALLED
    try:
        round3_compat._INSTALLED = False
        round3_compat.install_round3_compat()
        markup = round3_runtime._comment_preview_markup(
            {
                "updates_count": 12,
                "project_query": "135",
                "digest": "1234567890abcdef12",
            }
        )
        callback = markup["inline_keyboard"][0][0]["callback_data"]
        assert len(callback.encode("utf-8")) <= 64
        assert callback.split(":") == [
            "agent",
            "comment_sync",
            "confirm",
            "v1",
            "1234567890abcdef12",
            "12",
            "135",
        ]
    finally:
        round3_runtime._comment_preview_markup = original
        round3_compat._INSTALLED = original_installed


def test_round3_runtime_contains_supplier_and_failed_artifact_guards() -> None:
    source = inspect.getsource(round3_runtime.install_round3_runtime)
    assert '"фабрик"' in source
    assert '"04 Прайсы фабрик"' in source
    assert '"supplier_offer"' in source
    assert '"failed", "pending"' in source
    assert "Сделка Kommo" in source


def test_service_account_quota_error_is_detected() -> None:
    exc = RuntimeError("upload failed")
    exc.__cause__ = RuntimeError(
        "Service Accounts do not have storage quota. Use shared drives or OAuth."
    )
    assert round3_runtime._is_service_account_quota_error(exc) is True
