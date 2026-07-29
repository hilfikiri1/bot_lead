"""Project supplier registry, inquiry tracking and safe offer comparison."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.supplier_workspace import ProjectSupplier, SupplierInquiry, SupplierOffer


@dataclass
class OfferComparisonRow:
    supplier_id: int
    supplier_name: str
    offer_id: int
    currency: str
    incoterm: str | None
    named_place: str | None
    unit_price: Decimal | None
    total_price: Decimal | None
    moq: str | None
    lead_time_days: int | None
    warranty_months: int | None
    payment_terms: str | None
    certifications: list[str] = field(default_factory=list)
    deviations: list[str] = field(default_factory=list)


@dataclass
class OfferComparison:
    lead_id: int
    rows: list[OfferComparisonRow]
    comparable_groups: dict[str, list[int]]
    warnings: list[str]
    conclusions: list[str]


def _clean(value: Any, *, limit: int = 2000) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _identity_key(name: str, source_url: str | None = None) -> str:
    normalized = re.sub(r"\W+", "", name.casefold())
    host = ""
    if source_url:
        try:
            host = (urlparse(source_url).netloc or "").casefold()
        except Exception:
            host = ""
    raw = f"{normalized}:{host}:{_clean(source_url, limit=500).casefold()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def infer_platform(source_url: str | None) -> str | None:
    host = ""
    try:
        host = (urlparse(str(source_url or "")).netloc or "").casefold()
    except Exception:
        return None
    if "1688.com" in host:
        return "1688"
    if "alibaba.com" in host:
        return "Alibaba"
    if "made-in-china.com" in host:
        return "Made-in-China"
    if "globalsources.com" in host:
        return "Global Sources"
    return host or None


def parse_supplier_line(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in str(value or "").split("|")]
    if not parts or not parts[0]:
        raise ValueError("Укажите название фабрики.")
    name = _clean(parts[0], limit=255)
    source_url = _clean(parts[1], limit=2000) if len(parts) > 1 else ""
    contact = _clean(parts[2], limit=500) if len(parts) > 2 else ""
    notes = _clean(" | ".join(parts[3:]), limit=3000) if len(parts) > 3 else ""
    if source_url and not re.match(r"^https?://", source_url, flags=re.I):
        if any(domain in source_url.casefold() for domain in ("1688.com", "alibaba.com", "made-in-china.com")):
            source_url = "https://" + source_url
        else:
            contact = " | ".join(part for part in (source_url, contact) if part)
            source_url = ""
    return {
        "name": name,
        "source_url": source_url or None,
        "contact_value": contact or None,
        "notes": notes or None,
        "platform": infer_platform(source_url),
    }


def _decimal(value: Any) -> Decimal | None:
    raw = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not raw or raw in {"-", "—", "none", "null"}:
        return None
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Не удалось распознать цену: {value}") from exc


def _integer(value: Any) -> int | None:
    raw = re.sub(r"\D", "", str(value or ""))
    return int(raw) if raw else None


def parse_offer_line(value: str) -> dict[str, Any]:
    """Parse a compact offer line.

    Preferred format:
    currency | incoterm | unit price | total price | MOQ | lead days |
    warranty months | payment | certificates | notes
    """
    parts = [part.strip() for part in str(value or "").split("|")]
    if len(parts) < 4:
        raise ValueError(
            "Нужно минимум: валюта | Incoterm | цена за единицу | общая цена."
        )
    currency = re.sub(r"[^A-Za-z]", "", parts[0]).upper() or "USD"
    if len(currency) > 8:
        raise ValueError("Некорректная валюта.")
    incoterm_raw = _clean(parts[1], limit=100).upper()
    incoterm_match = re.match(r"^(EXW|FOB|CIF|CFR|DAP|DPU|DDP|FCA|CPT|CIP)(?:\s+(.+))?$", incoterm_raw)
    incoterm = incoterm_match.group(1) if incoterm_match else (incoterm_raw or None)
    named_place = incoterm_match.group(2).title() if incoterm_match and incoterm_match.group(2) else None
    certifications = [
        item.strip().upper()
        for item in re.split(r"[,;/]", parts[8] if len(parts) > 8 else "")
        if item.strip()
    ]
    return {
        "currency": currency,
        "incoterm": incoterm,
        "named_place": named_place,
        "unit_price": _decimal(parts[2]),
        "total_price": _decimal(parts[3]),
        "moq": _clean(parts[4], limit=255) or None if len(parts) > 4 else None,
        "lead_time_days": _integer(parts[5]) if len(parts) > 5 else None,
        "warranty_months": _integer(parts[6]) if len(parts) > 6 else None,
        "payment_terms": _clean(parts[7], limit=500) or None if len(parts) > 7 else None,
        "certifications_json": certifications,
        "notes": _clean(" | ".join(parts[9:]), limit=3000) or None if len(parts) > 9 else None,
    }


async def create_supplier(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
    internal_lead_number: str | None,
    name: str,
    source_url: str | None = None,
    contact_value: str | None = None,
    notes: str | None = None,
    platform: str | None = None,
    telegram_user_id: int | None = None,
) -> ProjectSupplier:
    clean_name = _clean(name, limit=255)
    if not clean_name:
        raise ValueError("Название фабрики обязательно.")
    identity = _identity_key(clean_name, source_url)
    existing = (
        await db.execute(
            select(ProjectSupplier).where(
                ProjectSupplier.kommo_lead_id == int(kommo_lead_id),
                ProjectSupplier.identity_key == identity,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    row = ProjectSupplier(
        kommo_lead_id=int(kommo_lead_id),
        internal_lead_number=_clean(internal_lead_number, limit=16) or None,
        name=clean_name,
        identity_key=identity,
        platform=platform or infer_platform(source_url),
        source_url=_clean(source_url, limit=2000) or None,
        contact_value=_clean(contact_value, limit=500) or None,
        notes=_clean(notes, limit=3000) or None,
        created_by_telegram_user_id=telegram_user_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def list_suppliers(db: AsyncSession, *, kommo_lead_id: int) -> list[ProjectSupplier]:
    result = await db.execute(
        select(ProjectSupplier)
        .where(ProjectSupplier.kommo_lead_id == int(kommo_lead_id))
        .order_by(ProjectSupplier.status.asc(), ProjectSupplier.created_at.asc())
    )
    return list(result.scalars().all())


async def get_supplier(db: AsyncSession, supplier_id: int) -> ProjectSupplier | None:
    return await db.get(ProjectSupplier, int(supplier_id))


async def record_inquiry_sent(
    db: AsyncSession,
    *,
    supplier_id: int,
    telegram_user_id: int | None = None,
    body: str | None = None,
    channel: str | None = None,
    followup_days: int = 3,
) -> SupplierInquiry:
    supplier = await get_supplier(db, supplier_id)
    if supplier is None:
        raise ValueError("Фабрика не найдена.")
    now = datetime.now(timezone.utc)
    inquiry = SupplierInquiry(
        supplier_id=int(supplier.id),
        kommo_lead_id=int(supplier.kommo_lead_id),
        channel=_clean(channel or supplier.contact_channel or "manual", limit=64),
        body=_clean(body, limit=15_000) or None,
        status="sent",
        sent_at=now,
        due_at=now + timedelta(days=max(1, min(int(followup_days), 30))),
        created_by_telegram_user_id=telegram_user_id,
    )
    supplier.status = "inquiry_sent"
    supplier.last_contact_at = now
    supplier.next_followup_at = inquiry.due_at
    db.add(inquiry)
    await db.commit()
    await db.refresh(inquiry)
    return inquiry


async def create_offer(
    db: AsyncSession,
    *,
    supplier_id: int,
    telegram_user_id: int | None = None,
    **values: Any,
) -> SupplierOffer:
    supplier = await get_supplier(db, supplier_id)
    if supplier is None:
        raise ValueError("Фабрика не найдена.")
    offer = SupplierOffer(
        supplier_id=int(supplier.id),
        kommo_lead_id=int(supplier.kommo_lead_id),
        title=_clean(values.get("title"), limit=500) or None,
        currency=_clean(values.get("currency") or "USD", limit=8).upper(),
        incoterm=_clean(values.get("incoterm"), limit=32).upper() or None,
        named_place=_clean(values.get("named_place"), limit=255) or None,
        unit_price=_decimal(values.get("unit_price")),
        total_price=_decimal(values.get("total_price")),
        moq=_clean(values.get("moq"), limit=255) or None,
        lead_time_days=_integer(values.get("lead_time_days")),
        warranty_months=_integer(values.get("warranty_months")),
        payment_terms=_clean(values.get("payment_terms"), limit=500) or None,
        packaging=_clean(values.get("packaging"), limit=3000) or None,
        certifications_json=list(values.get("certifications_json") or []),
        key_components_json=dict(values.get("key_components_json") or {}),
        deviations_json=list(values.get("deviations_json") or []),
        source_artifact_id=values.get("source_artifact_id"),
        source_url=_clean(values.get("source_url"), limit=2000) or None,
        notes=_clean(values.get("notes"), limit=3000) or None,
        raw_json=dict(values.get("raw_json") or {}),
        received_at=values.get("received_at") or datetime.now(timezone.utc),
        created_by_telegram_user_id=telegram_user_id,
    )
    supplier.status = "offer_received"
    supplier.next_followup_at = None
    db.add(offer)
    await db.commit()
    await db.refresh(offer)
    return offer


async def list_offers(db: AsyncSession, *, kommo_lead_id: int) -> list[tuple[SupplierOffer, ProjectSupplier]]:
    result = await db.execute(
        select(SupplierOffer, ProjectSupplier)
        .join(ProjectSupplier, ProjectSupplier.id == SupplierOffer.supplier_id)
        .where(SupplierOffer.kommo_lead_id == int(kommo_lead_id))
        .order_by(desc(SupplierOffer.received_at), desc(SupplierOffer.id))
    )
    return list(result.all())


def compare_offer_records(
    *,
    lead_id: int,
    records: list[tuple[SupplierOffer, ProjectSupplier]],
) -> OfferComparison:
    rows: list[OfferComparisonRow] = []
    for offer, supplier in records:
        rows.append(
            OfferComparisonRow(
                supplier_id=int(supplier.id),
                supplier_name=str(supplier.name),
                offer_id=int(offer.id),
                currency=str(offer.currency or "USD").upper(),
                incoterm=str(offer.incoterm or "").upper() or None,
                named_place=offer.named_place,
                unit_price=offer.unit_price,
                total_price=offer.total_price,
                moq=offer.moq,
                lead_time_days=offer.lead_time_days,
                warranty_months=offer.warranty_months,
                payment_terms=offer.payment_terms,
                certifications=[str(value) for value in (offer.certifications_json or [])],
                deviations=[str(value) for value in (offer.deviations_json or [])],
            )
        )
    groups: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        key = f"{row.currency}:{row.incoterm or 'UNKNOWN'}:{(row.named_place or '').casefold()}"
        groups.setdefault(key, []).append(index)
    warnings: list[str] = []
    if len(groups) > 1:
        warnings.append("Предложения имеют разные валюты, Incoterm или named place; глобально ранжировать цену нельзя.")
    if any(not row.incoterm for row in rows):
        warnings.append("У части предложений не указан Incoterm.")
    if any(not row.certifications for row in rows):
        warnings.append("У части фабрик не подтверждены сертификаты.")
    conclusions: list[str] = []
    for key, indexes in groups.items():
        comparable = [rows[index] for index in indexes]
        priced = [row for row in comparable if row.total_price is not None]
        metric = "общей цене"
        if not priced:
            priced = [row for row in comparable if row.unit_price is not None]
            metric = "цене за единицу"
        if len(priced) >= 2:
            best = min(priced, key=lambda row: row.total_price if row.total_price is not None else row.unit_price)
            value = best.total_price if best.total_price is not None else best.unit_price
            conclusions.append(
                f"В группе {key} минимальная {metric}: {best.supplier_name} — {value} {best.currency}."
            )
        lead_times = [row for row in comparable if row.lead_time_days is not None]
        if len(lead_times) >= 2:
            fastest = min(lead_times, key=lambda row: row.lead_time_days or 999999)
            conclusions.append(
                f"Самый короткий срок в группе {key}: {fastest.supplier_name} — {fastest.lead_time_days} дней."
            )
    return OfferComparison(
        lead_id=int(lead_id),
        rows=rows,
        comparable_groups=groups,
        warnings=list(dict.fromkeys(warnings)),
        conclusions=conclusions,
    )


async def compare_offers(db: AsyncSession, *, kommo_lead_id: int) -> OfferComparison:
    return compare_offer_records(
        lead_id=int(kommo_lead_id),
        records=await list_offers(db, kommo_lead_id=int(kommo_lead_id)),
    )


def _money(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "—"
    return f"{value.normalize()} {currency}"


def format_workspace(
    *,
    lead_name: str,
    suppliers: list[ProjectSupplier],
    offer_count: int,
) -> str:
    inquiry_sent = sum(1 for supplier in suppliers if supplier.status == "inquiry_sent")
    offer_received = sum(1 for supplier in suppliers if supplier.status == "offer_received")
    waiting = sum(
        1
        for supplier in suppliers
        if supplier.next_followup_at and supplier.next_followup_at > datetime.now(timezone.utc)
    )
    overdue = sum(
        1
        for supplier in suppliers
        if supplier.next_followup_at and supplier.next_followup_at <= datetime.now(timezone.utc)
    )
    lines = [
        f"<b>🏭 Фабрики · {html.escape(lead_name)}</b>",
        "",
        f"Всего фабрик: <b>{len(suppliers)}</b>",
        f"Запрос отправлен: <b>{inquiry_sent}</b>",
        f"Получено предложений: <b>{offer_count}</b>",
        f"Фабрик с предложением: <b>{offer_received}</b>",
        f"Ждём ответа: <b>{waiting}</b>",
        f"Просрочен ответ фабрики: <b>{overdue}</b>",
    ]
    if suppliers:
        lines.extend(["", "<b>Производители</b>"])
        for index, supplier in enumerate(suppliers[:20], 1):
            status = {
                "candidate": "кандидат",
                "inquiry_sent": "ждём ответ",
                "offer_received": "есть предложение",
                "rejected": "отклонён",
            }.get(supplier.status, supplier.status)
            lines.append(
                f"{index}. <b>{html.escape(supplier.name)}</b> · {html.escape(status)}"
                + (f" · {html.escape(supplier.platform)}" if supplier.platform else "")
            )
            if supplier.next_followup_at:
                lines.append(
                    f"   follow-up: {supplier.next_followup_at.astimezone().strftime('%d.%m %H:%M')}"
                )
    else:
        lines.extend(["", "Фабрики пока не добавлены."])
    return "\n".join(lines)[:4000]


def format_comparison(comparison: OfferComparison, *, lead_name: str) -> str:
    lines = [f"<b>📊 Сравнение предложений · {html.escape(lead_name)}</b>", ""]
    if not comparison.rows:
        lines.append("Предложений пока нет.")
        return "\n".join(lines)
    for index, row in enumerate(comparison.rows[:20], 1):
        term = " ".join(part for part in (row.incoterm, row.named_place) if part) or "условия не указаны"
        lines.extend(
            [
                f"<b>{index}. {html.escape(row.supplier_name)}</b>",
                f"Цена: {_money(row.total_price, row.currency)} total · {_money(row.unit_price, row.currency)} / ед.",
                f"Условия: {html.escape(term)}",
                f"MOQ: {html.escape(str(row.moq or '—'))} · срок: {row.lead_time_days or '—'} дн. · гарантия: {row.warranty_months or '—'} мес.",
                f"Оплата: {html.escape(str(row.payment_terms or '—'))}",
                f"Сертификаты: {html.escape(', '.join(row.certifications) if row.certifications else 'не подтверждены')}",
                "",
            ]
        )
    if comparison.warnings:
        lines.append("<b>⚠️ Ограничения сравнения</b>")
        lines.extend(f"• {html.escape(item)}" for item in comparison.warnings)
        lines.append("")
    if comparison.conclusions:
        lines.append("<b>Выводы</b>")
        lines.extend(f"• {html.escape(item)}" for item in comparison.conclusions)
    return "\n".join(lines)[:4000]
