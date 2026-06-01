"""
admin.py — Basic admin endpoints to view CRM data.
Secure with a simple token header in production.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Client, Lead, VoiceNote, AIReport, Action

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/leads")
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
            "id": l.id,
            "client": l.client.name if l.client else None,
            "company": l.client.company if l.client else None,
            "product_requested": l.product_requested,
            "budget": l.budget,
            "country": l.country,
            "status": l.status,
            "priority": l.priority,
            "next_action": l.next_action,
            "created_at": l.created_at.isoformat(),
        }
        for l in leads
    ]


@router.get("/leads/{lead_id}")
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
        "client": {
            "id": lead.client.id if lead.client else None,
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
                "id": vn.id,
                "transcript": vn.transcript,
                "language": vn.language,
                "audio_url": vn.audio_url,
                "created_at": vn.created_at.isoformat(),
                "ai_report": {
                    "id": vn.ai_report.id,
                    "conversation_summary": vn.ai_report.conversation_summary,
                    "recommended_next_step": vn.ai_report.recommended_next_step,
                    "confidence_score": vn.ai_report.confidence_score,
                    "needs_human_review": vn.ai_report.needs_human_review,
                    "email_subject": vn.ai_report.email_subject,
                    "whatsapp_message": vn.ai_report.whatsapp_message,
                    "calendar_title": vn.ai_report.calendar_title,
                    "calendar_start_time": vn.ai_report.calendar_start_time,
                } if vn.ai_report else None,
            }
            for vn in lead.voice_notes
        ],
        "actions": [
            {
                "id": a.id,
                "action_type": a.action_type,
                "status": a.status,
                "approved_by_user": a.approved_by_user,
                "executed_at": a.executed_at.isoformat() if a.executed_at else None,
            }
            for a in lead.actions
        ],
        "created_at": lead.created_at.isoformat(),
    }


@router.get("/clients")
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
            "id": c.id,
            "name": c.name,
            "company": c.company,
            "email": c.email,
            "phone": c.phone,
            "language": c.language,
            "created_at": c.created_at.isoformat(),
        }
        for c in clients
    ]


@router.get("/reports")
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
            "id": r.id,
            "voice_note_id": r.voice_note_id,
            "conversation_summary": r.conversation_summary,
            "confidence_score": r.confidence_score,
            "needs_human_review": r.needs_human_review,
            "created_at": r.created_at.isoformat(),
        }
        for r in reports
    ]
