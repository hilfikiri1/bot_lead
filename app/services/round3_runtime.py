"""Round-three production fixes from the full operator audit.

This module is installed after diagnostics hardening and therefore patches the
final production behavior. It keeps all writes behind existing confirmation
buttons.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from typing import Any

from app.agent import project_drive, project_snapshot, service as agent_service
from app.agent.contracts import AgentReply
from app.config import get_settings
from app.services import (
    comment_sync_service,
    diagnostics_hardening_runtime,
    drive_diagnostics,
    google_drive_service,
    identity_service,
    project_artifact_service,
    project_link_service,
    system_diagnostics,
)

logger = logging.getLogger(__name__)
settings = get_settings()
_INSTALLED = False

_COUNTRY_NAMES = {
    "PL": "01 Польша",
    "UA": "02 Украина",
    "DE": "03 Германия",
    "LV": "04 Латвия",
    "BG": "05 Болгария",
    "ES": "06 Испания",
    "OTHER": "07 Другие страны",
    "LT": "07 Другие страны",
    "BY": "07 Другие страны",
    "EE": "07 Другие страны",
}
_COUNTRY_ALIASES = {
    "01 Польша": ("01 Польша", "Польша"),
    "02 Украина": ("02 Украина", "Украина"),
    "03 Германия": ("03 Германия", "Германия"),
    "04 Латвия": ("04 Латвия", "Латвия"),
    "05 Болгария": ("05 Болгария", "Болгария"),
    "06 Испания": ("06 Испания", "Испания"),
    "07 Другие страны": ("07 Другие страны", "Другие страны"),
}
_COMMENT_RE = re.compile(
    r"^\s*/(?:comment_sync|comments_sync|sync_comments)(?:@\w+)?(?:\s+(\d+))?\s*$",
    flags=re.I,
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _drive_auth_mode() -> str:
    raw = os.getenv("GOOGLE_DRIVE_AUTH_MODE", "service_account").strip().casefold()
    if raw in {"oauth", "oauth_refresh", "oauth_refresh_token"}:
        return "oauth_refresh_token"
    return "service_account"


def _oauth_ready() -> bool:
    return bool(
        settings.google_client_id.strip()
        and settings.google_client_secret.strip()
        and settings.google_refresh_token.strip()
    )


def _build_oauth_drive_service():
    if not _oauth_ready():
        raise google_drive_service.GoogleDriveError(
            "Для OAuth Drive задайте GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET и GOOGLE_REFRESH_TOKEN."
        )
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials(
        token=None,
        refresh_token=settings.google_refresh_token.strip(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id.strip(),
        client_secret=settings.google_client_secret.strip(),
        scopes=[google_drive_service.DRIVE_SCOPE],
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _http_error_text(exc: Exception) -> str:
    cause = getattr(exc, "__cause__", None)
    target = cause or exc
    content = getattr(target, "content", b"")
    if isinstance(content, (bytes, bytearray)):
        try:
            content = content.decode("utf-8")
        except Exception:
            content = str(content)
    return (str(content or "") + " " + str(target or "")).casefold()


def _is_service_account_quota_error(exc: Exception) -> bool:
    category = str(getattr(exc, "category", "") or "")
    text = _http_error_text(exc)
    return category == "service_account_no_storage_quota" or (
        "service account" in text
        and "storage quota" in text
    )


async def _oauth_upload_file(
    *,
    parent_folder_id: str,
    filename: str,
    content: bytes,
    mime_type: str,
) -> dict[str, Any]:
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaInMemoryUpload

    service = _build_oauth_drive_service()
    media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
    body = {"name": filename[:255], "parents": [parent_folder_id]}

    def _call():
        return (
            service.files()
            .create(
                body=body,
                media_body=media,
                fields="id,name,webViewLink,mimeType,size",
                supportsAllDrives=True,
            )
            .execute()
        )

    try:
        return await google_drive_service._run(_call)
    except HttpError as exc:
        raise google_drive_service._http_error(exc) from exc


def _country_aliases(name: str) -> tuple[str, ...]:
    return _COUNTRY_ALIASES.get(str(name or ""), (str(name or ""),))


def _format_comment_preview(report: dict[str, Any]) -> str:
    updates = list(report.get("updates") or [])
    lines = [
        "<b>📝 СИНХРОНИЗАЦИЯ КОММЕНТАРИЕВ</b>",
        "",
        f"Воронка: <b>{html.escape(str(report.get('pipeline_name') or '—'))}</b>",
        f"Проверено строк: <b>{int(report.get('rows_scanned') or 0)}</b>",
        f"Будет обновлено X: <b>{len(updates)}</b>",
        f"Не найдено в Kommo: <b>{len(report.get('missing_in_kommo') or [])}</b>",
        f"Неоднозначных номеров: <b>{len(report.get('ambiguous') or [])}</b>",
        f"Без содержательного примечания: <b>{len(report.get('no_meaningful_note') or [])}</b>",
        "",
        "🔒 Изменяется только колонка <b>X</b>. Колонки W и Y не меняются.",
    ]
    if updates:
        lines.extend(["", "<b>Предлагаемые изменения:</b>"])
        for item in updates[:12]:
            old = _clean(item.get("old_comment")) or "—"
            new = _clean(item.get("new_comment")) or "—"
            lines.append(
                f"• строка {int(item.get('row_number') or 0)} · №{html.escape(str(item.get('lead_number') or '—'))}\n"
                f"  было: {html.escape(old[:160])}\n"
                f"  станет: <b>{html.escape(new[:240])}</b>"
            )
        if len(updates) > 12:
            lines.append(f"<i>…и ещё {len(updates) - 12}</i>")
    else:
        lines.extend(["", "✅ Актуальных изменений для X нет."])
    return "\n".join(lines)[:4000]


def _comment_preview_markup(report: dict[str, Any]) -> dict[str, Any] | None:
    count = int(report.get("updates_count") or 0)
    if not count:
        return None
    query = str(report.get("project_query") or "all")
    digest = str(report.get("digest") or "")
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"✅ Обновить X ({count})",
                    "callback_data": f"agent:comment_sync:confirm:{digest}:{count}:{query}",
                },
                {"text": "❌ Отмена", "callback_data": "agent:comment_sync:cancel"},
            ]
        ]
    }


def _format_comment_result(result: dict[str, Any]) -> str:
    if result.get("stale"):
        return (
            "⚠️ Данные Kommo или Google Sheets изменились после предпросмотра. "
            "Ничего не записано. Запусти <code>/comment_sync</code> ещё раз."
        )
    updated = int(result.get("updated_count") or 0)
    skipped = list(result.get("skipped") or [])
    lines = [
        "<b>✅ СИНХРОНИЗАЦИЯ X ЗАВЕРШЕНА</b>",
        "",
        f"Обновлено строк: <b>{updated}</b>",
        f"Пропущено после повторной проверки: <b>{len(skipped)}</b>",
        "Колонки W и Y не изменялись.",
    ]
    if skipped:
        lines.extend(["", "<b>Пропущены:</b>"])
        for item in skipped[:8]:
            lines.append(
                f"• строка {item.get('row_number') or '—'} · {html.escape(str(item.get('reason') or 'изменена вручную'))}"
            )
    return "\n".join(lines)[:4000]


def install_round3_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # 1) Use the numbered country folders that already exist in the B&BS Drive.
    google_drive_service.COUNTRY_FOLDER_NAMES.update(_COUNTRY_NAMES)
    original_find_named_folder = diagnostics_hardening_runtime._find_named_folder

    async def find_named_folder_with_aliases(
        *, parent_id: str, name: str
    ) -> dict[str, Any] | None:
        aliases = _country_aliases(name)
        if len(aliases) == 1:
            return await original_find_named_folder(parent_id=parent_id, name=name)
        folders = await diagnostics_hardening_runtime._list_child_folders(parent_id)
        by_name = {str(item.get("name") or ""): item for item in folders}
        for alias in aliases:
            if alias in by_name:
                return by_name[alias]
        return None

    diagnostics_hardening_runtime._find_named_folder = find_named_folder_with_aliases
    google_drive_service.find_country_folder = find_named_folder_with_aliases

    # 2) Support explicit OAuth Drive mode for My Drive uploads. Service accounts
    # can create folders in a shared My Drive folder but have no binary storage quota.
    original_build_service = google_drive_service._build_service

    def build_drive_service_by_mode():
        if _drive_auth_mode() == "oauth_refresh_token":
            return _build_oauth_drive_service()
        return original_build_service()

    google_drive_service._build_service = build_drive_service_by_mode

    original_classify_http_error = drive_diagnostics.classify_http_error

    def classify_http_error_with_quota(exc):
        text = _http_error_text(exc)
        if "service account" in text and "storage quota" in text:
            message, hint = drive_diagnostics._CATEGORY_MESSAGES[
                "service_account_no_storage_quota"
            ]
            return drive_diagnostics.DriveErrorInfo(
                "service_account_no_storage_quota",
                message,
                status_code=int(getattr(getattr(exc, "resp", None), "status", 403) or 403),
                retryable=False,
                user_hint=hint,
            )
        return original_classify_http_error(exc)

    drive_diagnostics._CATEGORY_MESSAGES["service_account_no_storage_quota"] = (
        "Папка находится в My Drive: service account может создавать папки, но не имеет квоты для загрузки файлов.",
        "Используйте GOOGLE_DRIVE_AUTH_MODE=oauth_refresh_token или перенесите базу в настоящий Shared Drive.",
    )
    drive_diagnostics.classify_http_error = classify_http_error_with_quota

    original_upload_file = google_drive_service.upload_file

    async def upload_file_with_oauth_fallback(**kwargs):
        try:
            return await original_upload_file(**kwargs)
        except Exception as exc:
            if (
                _drive_auth_mode() == "service_account"
                and _oauth_ready()
                and _is_service_account_quota_error(exc)
            ):
                logger.warning("Drive service-account quota hit; retrying upload with OAuth")
                return await _oauth_upload_file(**kwargs)
            raise

    google_drive_service.upload_file = upload_file_with_oauth_fallback

    # 3) Diagnostics must distinguish folder permissions from actual file-upload ability.
    original_drive_check = system_diagnostics._drive_check

    async def drive_check_with_storage_truth() -> dict[str, Any]:
        result = await original_drive_check()
        data = dict(result.get("data") or {})
        mode = _drive_auth_mode()
        data["auth_mode"] = mode
        data["oauth_refresh_credentials_ready"] = _oauth_ready()
        projects = dict((data.get("folder_capabilities") or {}).get("projects") or {})
        root_id = settings.google_drive_projects_folder_id.strip()
        duplicates: list[dict[str, Any]] = []
        if root_id:
            try:
                folders = await diagnostics_hardening_runtime._list_child_folders(root_id)
                names = {str(item.get("name") or "") for item in folders}
                for numbered, aliases in _COUNTRY_ALIASES.items():
                    present = [alias for alias in aliases if alias in names]
                    if len(present) > 1:
                        duplicates.append({"country": numbered, "folders": present})
            except Exception:
                pass
        data["duplicate_country_folders"] = duplicates
        result["data"] = data

        if mode == "service_account" and projects and not projects.get("drive_id_present"):
            result["status"] = "FAIL"
            result["detail"] = (
                "Drive читается и папки создаются, но projects folder находится в My Drive. "
                "Service account не имеет квоты для загрузки файлов."
            )
            result["recommendation"] = (
                "Задайте GOOGLE_DRIVE_AUTH_MODE=oauth_refresh_token и OAuth credentials с Drive scope, "
                "либо используйте настоящий Shared Drive."
            )
        elif duplicates and result.get("status") == "PASS":
            result["status"] = "WARN"
            result["detail"] = "Drive доступен, но найдены дубли папок стран."
            result["recommendation"] = (
                "Новые проекты будут использовать папки с числовым префиксом; пустые дубли удалите вручную."
            )
        return result

    system_diagnostics._drive_check = drive_check_with_storage_truth

    # 4) Re-running confirmed project creation moves an already linked project into
    # the numbered country folder. The empty duplicate country folder is not deleted.
    original_execute_drive_project = project_drive.execute_drive_project

    async def execute_drive_project_in_numbered_country(
        db: Any, *, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = await original_execute_drive_project(db, payload=payload)
        country = str(payload.get("country_code") or "OTHER").upper()
        country_folder = await diagnostics_hardening_runtime._ensure_country_folder(
            root_id=settings.google_drive_projects_folder_id.strip(),
            country_code=country,
        )
        link = await project_link_service.get_by_kommo_lead_id(
            db, int(payload["kommo_lead_id"])
        )
        if link and link.drive_folder_id:
            folder = await diagnostics_hardening_runtime._verify_folder_access(
                str(link.drive_folder_id)
            )
            target_id = str(country_folder.get("id") or "")
            if target_id and target_id not in [str(x) for x in folder.get("parents") or []]:
                moved, changed, warning = await diagnostics_hardening_runtime._move_folder(
                    folder=folder,
                    target_parent_id=target_id,
                )
                metadata = dict(link.metadata_json or {})
                metadata.update(
                    {
                        "country_folder_id": target_id,
                        "country_folder_name": country_folder.get("name"),
                        "moved_to_numbered_country": bool(changed),
                        "country_move_warning": warning,
                    }
                )
                link.metadata_json = metadata
                link.drive_folder_url = str(
                    moved.get("webViewLink") or moved.get("url") or link.drive_folder_url or ""
                )
                await db.commit()
                result["moved_to_numbered_country"] = bool(changed)
                result["country_move_warning"] = warning
        result["country_folder"] = country_folder.get("name")
        return result

    project_drive.execute_drive_project = execute_drive_project_in_numbered_country

    # 5) Captions such as "фабрика" are an explicit supplier-document signal.
    original_classify_artifact = project_artifact_service.classify_artifact

    def classify_artifact_with_supplier_hint(
        *, filename: str, mime_type: str, caption: str | None, kind: str | None = None
    ):
        caption_text = _clean(caption).casefold()
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
        if supplier_hint:
            return project_artifact_service.ArtifactClassification(
                "supplier_offer",
                "Предложение производителя",
                "04 Прайсы фабрик",
                "caption_supplier_hint",
                0.99,
            )
        return original_classify_artifact(
            filename=filename,
            mime_type=mime_type,
            caption=caption,
            kind=kind,
        )

    project_artifact_service.classify_artifact = classify_artifact_with_supplier_hint

    # 6) A failed/pending upload is not a Drive file. Keep it out of "Последние файлы"
    # and show a truthful blocker instead.
    original_build_snapshot = project_snapshot.build_snapshot

    async def build_snapshot_with_uploaded_files_only(*args, **kwargs):
        snapshot = await original_build_snapshot(*args, **kwargs)
        visible: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for item in snapshot.documents:
            status = str(item.get("status") or "").casefold()
            if status in {"uploaded", "uploaded_with_warnings", "external"}:
                visible.append(item)
            elif status in {"failed", "pending", "approved", "executing"}:
                failed.append(item)
        snapshot.documents = visible
        for item in failed[:3]:
            name = _clean(item.get("name")) or "файл"
            message = f"Файл не загружен в Drive: {name[:160]}"
            if message not in snapshot.blockers:
                snapshot.blockers.append(message)
        return snapshot

    project_snapshot.build_snapshot = build_snapshot_with_uploaded_files_only

    # 7) Make the Kommo entity explicit: it is a deal, with an optional linked project.
    original_format_snapshot = project_snapshot.format_snapshot

    def format_snapshot_as_deal_and_project(snapshot) -> str:
        text = original_format_snapshot(snapshot)
        lines = text.splitlines()
        internal = snapshot.identity.get("internal_lead_number")
        project_key = snapshot.identity.get("project_key")
        if internal and project_key:
            heading = (
                f"<b>💼 Сделка Kommo №{html.escape(str(internal))} · "
                f"проект {html.escape(str(project_key))}</b>"
            )
        elif internal:
            heading = f"<b>💼 Сделка Kommo №{html.escape(str(internal))}</b>"
        else:
            heading = "<b>💼 Сделка Kommo</b>"
        if lines:
            lines[0] = heading
        return "\n".join(lines)[:4000]

    project_snapshot.format_snapshot = format_snapshot_as_deal_and_project

    # 8) Manual /comment_sync command with a stable preview and second-source check.
    original_handle_message = agent_service.handle_message
    original_handle_callback = agent_service.handle_callback

    async def handle_message_with_comment_sync(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ) -> AgentReply:
        match = _COMMENT_RE.match(str(text or ""))
        if match:
            actor = identity_service.current_user()
            if actor is not None and not identity_service.can_write(actor):
                return AgentReply(
                    "🔒 Роль Viewer не может изменять Google Sheets.",
                    intent="comment_sync_denied",
                )
            query = match.group(1) or None
            report = await comment_sync_service.build_comment_sync_report(query)
            return AgentReply(
                _format_comment_preview(report),
                reply_markup=_comment_preview_markup(report),
                intent="comment_sync_preview",
                metadata={
                    "digest": report.get("digest"),
                    "updates_count": report.get("updates_count"),
                    "project_query": query,
                },
            )
        return await original_handle_message(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            text=text,
            source=source,
            allow_conversation_passthrough=allow_conversation_passthrough,
            active_kommo_lead_id=active_kommo_lead_id,
        )

    async def handle_callback_with_comment_sync(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ) -> AgentReply | None:
        if callback_data == "agent:comment_sync:cancel":
            return AgentReply("❌ Синхронизация комментариев отменена.", intent="comment_sync_cancelled")
        if callback_data.startswith("agent:comment_sync:confirm:"):
            actor = identity_service.current_user()
            if actor is not None and not identity_service.can_write(actor):
                return AgentReply(
                    "🔒 Роль Viewer не может изменять Google Sheets.",
                    intent="comment_sync_denied",
                )
            parts = callback_data.split(":", 6)
            if len(parts) != 7:
                return AgentReply("❌ Некорректная команда синхронизации X.")
            digest = parts[4]
            try:
                count = int(parts[5])
            except ValueError:
                return AgentReply("❌ Некорректное количество изменений X.")
            query = None if parts[6] == "all" else parts[6]
            result = await comment_sync_service.apply_confirmed_report(
                expected_digest=digest,
                expected_count=count,
                project_query=query,
            )
            return AgentReply(
                _format_comment_result(result),
                intent="comment_sync_applied" if not result.get("stale") else "comment_sync_stale",
            )
        return await original_handle_callback(
            db,
            callback_data=callback_data,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    agent_service.handle_message = handle_message_with_comment_sync
    agent_service.handle_callback = handle_callback_with_comment_sync
    logger.info("Round3 Drive, artifact truth and comment sync runtime installed")
