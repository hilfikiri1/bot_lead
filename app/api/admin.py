"""Protected diagnostic endpoints for local CRM data."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.models import AIReport, Client, Lead, VoiceNote

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()


def require_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    configured = settings.admin_api_key.strip()
    if not configured:
        # Do not silently expose personal CRM data when the key is missing.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API is disabled",
        )
    if not hmac.compare_digest(x_admin_key or "", configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin key",
        )


admin_auth = Depends(require_admin_key)


@router.get("/leads", dependencies=[admin_auth])
async def list_leads(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    result = await db.execute(
        select(Lead)
        .options(selectinload(Lead.client))
        .order_by(desc(Lead.created_at))
        .limit(limit)
        .offset(offset)
    )
    leads = result.scalars().all()
    return [
        {
            "id": item.id,
            "kommo_lead_id": item.kommo_lead_id,
            "client": item.client.name if item.client else None,
            "company": item.client.company if item.client else None,
            "product_requested": item.product_requested,
            "budget": item.budget,
            "country": item.country,
            "status": item.status,
            "priority": item.priority,
            "next_action": item.next_action,
            "created_at": item.created_at.isoformat(),
        }
        for item in leads
    ]


@router.get("/leads/{lead_id}", dependencies=[admin_auth])
async def get_lead(lead_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Lead)
        .where(Lead.id == lead_id)
        .options(
            selectinload(Lead.client),
            selectinload(Lead.voice_notes).selectinload(VoiceNote.ai_report),
            selectinload(Lead.actions),
        )
    )
    lead = result.scalar_one_or_none()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    return {
        "id": lead.id,
        "kommo_lead_id": lead.kommo_lead_id,
        "kommo_pipeline_id": lead.kommo_pipeline_id,
        "kommo_status_id": lead.kommo_status_id,
        "kommo_url": lead.kommo_url,
        "client": {
            "id": lead.client.id if lead.client else None,
            "kommo_contact_id": lead.client.kommo_contact_id if lead.client else None,
            "name": lead.client.name if lead.client else None,
            "company": lead.client.company if lead.client else None,
            "email": lead.client.email if lead.client else None,
            "phone": lead.client.phone if lead.client else None,
            "language": lead.client.language if lead.client else None,
        },
        "product_requested": lead.product_requested,
        "budget": lead.budget,
        "country": lead.country,
        "city": lead.city,
        "status": lead.status,
        "priority": lead.priority,
        "next_action": lead.next_action,
        "voice_notes": [
            {
                "id": note.id,
                "processing_status": note.processing_status,
                "processing_error": note.processing_error,
                "language": note.language,
                "created_at": note.created_at.isoformat(),
                # Transcript/audio are intentionally omitted from the list endpoint.
                "ai_report": {
                    "id": note.ai_report.id,
                    "conversation_summary": note.ai_report.conversation_summary,
                    "recommended_next_step": note.ai_report.recommended_next_step,
                    "confidence_score": note.ai_report.confidence_score,
                    "needs_human_review": note.ai_report.needs_human_review,
                }
                if note.ai_report
                else None,
            }
            for note in lead.voice_notes
        ],
        "actions": [
            {
                "id": action.id,
                "action_type": action.action_type,
                "status": action.status,
                "approved_by_user": action.approved_by_user,
                "error_message": action.error_message,
                "executed_at": action.executed_at.isoformat()
                if action.executed_at
                else None,
            }
            for action in lead.actions
        ],
        "created_at": lead.created_at.isoformat(),
    }


@router.get("/clients", dependencies=[admin_auth])
async def list_clients(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    result = await db.execute(
        select(Client).order_by(desc(Client.created_at)).limit(limit).offset(offset)
    )
    clients = result.scalars().all()
    return [
        {
            "id": client.id,
            "kommo_contact_id": client.kommo_contact_id,
            "name": client.name,
            "company": client.company,
            "email": client.email,
            "phone": client.phone,
            "language": client.language,
            "created_at": client.created_at.isoformat(),
        }
        for client in clients
    ]


@router.get("/reports", dependencies=[admin_auth])
async def list_reports(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
):
    result = await db.execute(
        select(AIReport).order_by(desc(AIReport.created_at)).limit(limit)
    )
    reports = result.scalars().all()
    return [
        {
            "id": report.id,
            "voice_note_id": report.voice_note_id,
            "conversation_summary": report.conversation_summary,
            "confidence_score": report.confidence_score,
            "needs_human_review": report.needs_human_review,
            "created_at": report.created_at.isoformat(),
        }
        for report in reports
    ]
