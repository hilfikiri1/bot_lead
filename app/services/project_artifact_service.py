"""Classify, name and audit files attached to B&BS projects."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project_artifact import ProjectArtifact
from app.models.project_link import ProjectLink
from app.services.google_drive_service import PROJECT_SUBFOLDERS, sanitize_filename


@dataclass(frozen=True)
class ArtifactClassification:
    artifact_type: str
    label: str
    subfolder_name: str
    source: str
    confidence: float


_RULES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "contract",
        "Договор",
        "10 Договоры, инвойсы и оплата",
        ("договор", "contract", "umowa", "协议", "контракт"),
    ),
    (
        "invoice",
        "Инвойс",
        "10 Договоры, инвойсы и оплата",
        ("invoice", "инвойс", "proforma", "pi ", "счёт", "счет", "faktura"),
    ),
    (
        "technical_spec",
        "Техническое задание",
        "02 Техническое задание",
        ("техническ", "техзадан", "тз ", "specification", "specyfikac", "spec "),
    ),
    (
        "commercial_offer",
        "Коммерческое предложение",
        "07 Коммерческие предложения",
        ("коммерческ", "кп ", "offer to client", "oferta dla klient"),
    ),
    (
        "supplier_offer",
        "Предложение производителя",
        "04 Прайсы фабрик",
        (
            "предложение производителя",
            "предложение фабрики",
            "supplier offer",
            "factory offer",
            "quotation",
            "quote",
            "报价",
        ),
    ),
    (
        "price_list",
        "Прайс производителя",
        "04 Прайсы фабрик",
        ("прайс", "price list", "cennik", "价格表", "price"),
    ),
    (
        "catalog",
        "Каталог производителя",
        "03 Поставщики и RFQ",
        ("каталог", "catalog", "katalog", "brochure", "产品目录"),
    ),
    (
        "certificate",
        "Сертификат",
        "08 Сертификаты и проверка",
        ("сертификат", "certificate", "certyfikat", "declaration", "ce ", "doc "),
    ),
    (
        "logistics",
        "Логистика и таможня",
        "09 Логистика и таможня",
        ("логист", "достав", "тамож", "packing list", "bill of lading", "transport"),
    ),
    (
        "calculation",
        "Расчёт или сравнение",
        "06 Расчёты и сравнение",
        ("расчёт", "расчет", "калькуляц", "сравнен", "calculation", "comparison"),
    ),
    (
        "client_request",
        "Запрос клиента",
        "01 Запрос клиента",
        ("запрос клиента", "request from client", "zapytanie klienta", "бриф"),
    ),
    (
        "supplier_rfq",
        "Запрос поставщику",
        "03 Поставщики и RFQ",
        ("rfq", "запрос поставщик", "запрос фабрик", "inquiry"),
    ),
)


def classify_artifact(
    *,
    filename: str,
    mime_type: str,
    caption: str | None,
    kind: str | None = None,
) -> ArtifactClassification:
    haystack = " ".join((caption or "", filename or "")).casefold()
    for artifact_type, label, subfolder, keywords in _RULES:
        if any(keyword in haystack for keyword in keywords):
            return ArtifactClassification(
                artifact_type,
                label,
                subfolder,
                "caption_filename_rule",
                0.96 if caption else 0.84,
            )

    lowered_mime = (mime_type or "").casefold()
    suffix = Path(filename or "").suffix.casefold()
    if kind == "photo" or lowered_mime.startswith("image/"):
        return ArtifactClassification(
            "photo",
            "Фото товара или образца",
            "05 Фото, видео и образцы",
            "mime_rule",
            0.92,
        )
    if lowered_mime.startswith("video/"):
        return ArtifactClassification(
            "video",
            "Видео товара или образца",
            "05 Фото, видео и образцы",
            "mime_rule",
            0.92,
        )
    if suffix in {".xlsx", ".xls", ".csv", ".ods"}:
        return ArtifactClassification(
            "spreadsheet",
            "Рабочая таблица",
            "06 Расчёты и сравнение",
            "extension_rule",
            0.70,
        )
    return ArtifactClassification(
        "document",
        "Документ проекта",
        PROJECT_SUBFOLDERS[0],
        "fallback",
        0.50,
    )


def suggested_filename(
    *,
    project_key: str,
    classification: ArtifactClassification,
    original_filename: str,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    original = sanitize_filename(Path(original_filename or "file").name)
    suffix = Path(original).suffix.lower()
    stem = Path(original).stem
    # Avoid repeating generic Telegram names while keeping a useful source hint.
    generic = {"document", "file", "photo", "image", "scan", "img"}
    source_hint = "" if stem.casefold() in generic else f" — {stem[:70]}"
    name = (
        f"{now.date().isoformat()} — {project_key} — "
        f"{classification.label}{source_hint}{suffix}"
    )
    return sanitize_filename(name)


async def create_pending(
    db: AsyncSession,
    *,
    link: ProjectLink,
    telegram_user_id: int,
    telegram_message_id: int | None,
    original_filename: str,
    suggested_name: str,
    mime_type: str,
    file_size: int,
    classification: ArtifactClassification,
    caption: str | None,
    preview_text: str,
    storage_path: str,
    metadata: dict[str, Any] | None = None,
) -> ProjectArtifact:
    record = ProjectArtifact(
        project_link_id=int(link.id),
        kommo_lead_id=int(link.kommo_lead_id),
        telegram_user_id=int(telegram_user_id),
        telegram_message_id=(
            int(telegram_message_id) if telegram_message_id is not None else None
        ),
        original_filename=sanitize_filename(original_filename),
        suggested_filename=sanitize_filename(suggested_name),
        mime_type=str(mime_type or "application/octet-stream")[:255],
        file_size=max(0, int(file_size)),
        artifact_type=classification.artifact_type,
        artifact_type_label=classification.label,
        classification_source=classification.source,
        subfolder_name=classification.subfolder_name,
        caption=(caption or None),
        preview_text=preview_text[:20_000],
        storage_path=storage_path,
        status="pending",
        metadata_json={
            "classification_confidence": classification.confidence,
            **(metadata or {}),
        },
    )
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if telegram_message_id is None:
            raise
        existing = await get_by_telegram_message(
            db,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
        )
        if existing is not None:
            return existing
        raise
    await db.refresh(record)
    return record


async def get_by_telegram_message(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_message_id: int,
) -> ProjectArtifact | None:
    return (
        await db.execute(
            select(ProjectArtifact).where(
                ProjectArtifact.telegram_user_id == int(telegram_user_id),
                ProjectArtifact.telegram_message_id == int(telegram_message_id),
            )
        )
    ).scalar_one_or_none()


async def get_artifact(
    db: AsyncSession,
    artifact_id: int,
    *,
    lock: bool = False,
) -> ProjectArtifact | None:
    query = select(ProjectArtifact).where(ProjectArtifact.id == int(artifact_id))
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def mark_uploaded(
    db: AsyncSession,
    *,
    artifact: ProjectArtifact,
    uploaded_by_telegram_user_id: int,
    uploaded: dict[str, Any],
    notion: dict[str, Any] | None,
    kommo_note_created: bool,
    warnings: list[str],
) -> ProjectArtifact:
    artifact.final_filename = str(
        uploaded.get("name") or artifact.suggested_filename
    )[:255]
    artifact.drive_file_id = (
        str(uploaded.get("id")) if uploaded.get("id") is not None else None
    )
    artifact.drive_file_url = uploaded.get("webViewLink")
    artifact.notion_page_id = (
        str((notion or {}).get("id")) if (notion or {}).get("id") else None
    )
    artifact.notion_page_url = (notion or {}).get("url")
    artifact.kommo_note_created = bool(kommo_note_created)
    artifact.warnings_json = warnings or []
    artifact.status = "uploaded_with_warnings" if warnings else "uploaded"
    artifact.uploaded_by_telegram_user_id = int(uploaded_by_telegram_user_id)
    artifact.uploaded_at = datetime.now(timezone.utc)
    # The temporary file may be deleted by the storage backend after upload.
    artifact.storage_path = None
    await db.commit()
    await db.refresh(artifact)
    return artifact


async def mark_failed(
    db: AsyncSession,
    *,
    artifact: ProjectArtifact,
    error: str,
) -> None:
    artifact.status = "failed"
    artifact.warnings_json = [str(error)[:1000]]
    artifact.storage_path = None
    await db.commit()


async def mark_cancelled(
    db: AsyncSession,
    *,
    artifact: ProjectArtifact,
    status: str = "rejected",
) -> None:
    artifact.status = status[:32]
    artifact.storage_path = None
    await db.commit()


async def recent_for_project(
    db: AsyncSession,
    kommo_lead_id: int,
    *,
    limit: int = 10,
) -> list[ProjectArtifact]:
    result = await db.execute(
        select(ProjectArtifact)
        .where(ProjectArtifact.kommo_lead_id == int(kommo_lead_id))
        .order_by(desc(ProjectArtifact.created_at))
        .limit(max(1, min(limit, 50)))
    )
    return list(result.scalars().all())


async def recent_for_leads(
    db: AsyncSession,
    kommo_lead_ids: Iterable[int],
    *,
    since: datetime | None = None,
    limit: int = 20,
) -> list[ProjectArtifact]:
    ids = [int(value) for value in kommo_lead_ids if int(value) > 0]
    if not ids:
        return []
    query = select(ProjectArtifact).where(ProjectArtifact.kommo_lead_id.in_(ids))
    if since is not None:
        query = query.where(ProjectArtifact.created_at >= since)
    result = await db.execute(
        query.order_by(desc(ProjectArtifact.created_at)).limit(max(1, min(limit, 100)))
    )
    return list(result.scalars().all())


async def count_recent_for_leads(
    db: AsyncSession,
    kommo_lead_ids: Iterable[int],
    *,
    hours: int = 24,
) -> int:
    ids = [int(value) for value in kommo_lead_ids if int(value) > 0]
    if not ids:
        return 0
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    return int(
        (
            await db.execute(
                select(func.count(ProjectArtifact.id)).where(
                    ProjectArtifact.kommo_lead_id.in_(ids),
                    ProjectArtifact.created_at >= since,
                    ProjectArtifact.status.in_(("uploaded", "uploaded_with_warnings")),
                )
            )
        ).scalar_one()
        or 0
    )
