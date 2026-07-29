"""CRUD and project_key helpers for ProjectLink."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project_link import ProjectLink

_COUNTRY_PHONE_PREFIX = {
    "+48": "PL",
    "+380": "UA",
    "+49": "DE",
    "+375": "BY",
    "+370": "LT",
    "+371": "LV",
    "+372": "EE",
}


def infer_country_code(
    *,
    lead: dict[str, Any] | None = None,
    explicit: str | None = None,
    saved: str | None = None,
) -> str:
    if saved:
        return saved.upper()[:8]
    if explicit:
        code = explicit.strip().upper()
        if len(code) == 2:
            return code
    if lead:
        custom = lead.get("custom_fields") or {}
        for key in ("country", "country_code", "страна"):
            val = custom.get(key) if isinstance(custom, dict) else None
            if val:
                token = str(val).strip().upper()
                if len(token) == 2:
                    return token
        contacts = lead.get("contacts") or []
        for contact in contacts:
            for phone in contact.get("phones") or []:
                phone_s = str(phone).replace(" ", "")
                for prefix, code in _COUNTRY_PHONE_PREFIX.items():
                    if phone_s.startswith(prefix):
                        return code
    return "OTHER"


def build_project_key(
    *,
    country_code: str,
    internal_lead_number: str | None,
    kommo_lead_id: int,
) -> str:
    country = (country_code or "OTHER").upper()[:8]
    if internal_lead_number and str(internal_lead_number).isdigit():
        return f"BBS-{country}-{int(internal_lead_number):04d}"
    return f"BBS-{country}-KOMMO-{int(kommo_lead_id)}"


async def get_by_kommo_lead_id(
    db: AsyncSession, kommo_lead_id: int
) -> ProjectLink | None:
    result = await db.execute(
        select(ProjectLink).where(
            ProjectLink.kommo_lead_id == int(kommo_lead_id),
            ProjectLink.status == "active",
        )
    )
    return result.scalar_one_or_none()


async def get_by_project_key(db: AsyncSession, project_key: str) -> ProjectLink | None:
    result = await db.execute(
        select(ProjectLink).where(ProjectLink.project_key == project_key)
    )
    return result.scalar_one_or_none()


async def upsert_link(
    db: AsyncSession,
    *,
    project_key: str,
    kommo_lead_id: int,
    internal_lead_number: str | None = None,
    kommo_lead_name: str | None = None,
    country_code: str | None = None,
    client_name: str | None = None,
    project_name: str | None = None,
    notion_project_page_id: str | None = None,
    notion_project_url: str | None = None,
    drive_folder_id: str | None = None,
    drive_folder_url: str | None = None,
    drive_folder_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ProjectLink:
    existing = await get_by_kommo_lead_id(db, kommo_lead_id)
    if existing is None:
        existing = await get_by_project_key(db, project_key)
    if existing:
        existing.project_key = project_key
        existing.internal_lead_number = internal_lead_number
        existing.kommo_lead_name = kommo_lead_name
        existing.country_code = country_code
        existing.client_name = client_name
        existing.project_name = project_name
        if notion_project_page_id:
            existing.notion_project_page_id = notion_project_page_id
        if notion_project_url:
            existing.notion_project_url = notion_project_url
        if drive_folder_id:
            existing.drive_folder_id = drive_folder_id
        if drive_folder_url:
            existing.drive_folder_url = drive_folder_url
        if drive_folder_name:
            existing.drive_folder_name = drive_folder_name
        if metadata:
            merged = dict(existing.metadata_json or {})
            merged.update(metadata)
            existing.metadata_json = merged
        existing.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(existing)
        return existing

    link = ProjectLink(
        project_key=project_key,
        internal_lead_number=internal_lead_number,
        kommo_lead_id=int(kommo_lead_id),
        kommo_lead_name=kommo_lead_name,
        country_code=country_code,
        client_name=client_name,
        project_name=project_name,
        notion_project_page_id=notion_project_page_id,
        notion_project_url=notion_project_url,
        drive_folder_id=drive_folder_id,
        drive_folder_url=drive_folder_url,
        drive_folder_name=drive_folder_name,
        metadata_json=metadata or {},
        status="active",
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link
