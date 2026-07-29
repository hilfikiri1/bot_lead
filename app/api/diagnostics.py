"""Protected read-only system diagnostics for Railway AI and support tooling."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import require_admin_key
from app.database import get_db
from app.services.system_diagnostics import run_system_diagnostics

router = APIRouter(prefix="/admin/diagnostics", tags=["diagnostics"])


@router.get("/run", dependencies=[Depends(require_admin_key)])
async def run_diagnostics(
    project: str | None = Query(default=None, max_length=120),
    recent_minutes: int = Query(default=60, ge=5, le=1440),
    db: AsyncSession = Depends(get_db),
):
    """Run the same safe audit as /diag and return the JSON report."""
    return await run_system_diagnostics(
        db,
        telegram_user_id=None,
        project_query=(project.strip() if project and project.strip() else None),
        recent_minutes=recent_minutes,
    )
