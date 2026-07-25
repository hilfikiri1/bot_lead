"""Catalog API for Chrome extension batch PDF generation."""

from __future__ import annotations

import hmac
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.catalog_schemas import (
    BatchCatalogRequest,
    BatchCatalogResponse,
    BatchJobStatusResponse,
)
from app.config import get_settings
from app.database import get_db
from app.models.catalog_job import CatalogJob
from app.tasks.catalog_tasks import process_catalog_batch

router = APIRouter(prefix="/api/catalog", tags=["catalog"])
settings = get_settings()


def require_catalog_api_key(
    authorization: str | None = Header(default=None),
    x_catalog_api_key: str | None = Header(default=None),
) -> None:
    configured = settings.catalog_api_key
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalog API is disabled",
        )

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_catalog_api_key:
        token = x_catalog_api_key.strip()

    if not hmac.compare_digest(token, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid catalog API key",
        )


catalog_auth = Depends(require_catalog_api_key)


@router.post("/batch", response_model=BatchCatalogResponse, dependencies=[catalog_auth])
async def create_batch_catalog_job(
    payload: BatchCatalogRequest,
    db: AsyncSession = Depends(get_db),
):
    if not settings.catalog_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Catalog is disabled")

    max_products = settings.catalog_max_products_per_batch
    if len(payload.products) > max_products:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {max_products} products per batch",
        )

    products_data = [item.model_dump(mode="json") for item in payload.products]
    job = CatalogJob(
        job_type="batch",
        telegram_user_id=payload.options.telegram_user_id,
        telegram_chat_id=payload.options.telegram_chat_id,
        source_url=payload.options.source_page_url,
        products_json=json.dumps(products_data, ensure_ascii=False),
        product_count=len(products_data),
        status="received",
        product_title=f"Подборка из {len(products_data)} товаров",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    mode = (settings.catalog_processing_mode or "celery").strip().lower()
    if mode == "celery":
        process_catalog_batch.delay(str(job.id))
    else:
        import asyncio

        from app.tasks.catalog_tasks import process_catalog_batch_async

        asyncio.create_task(process_catalog_batch_async(str(job.id)))

    return BatchCatalogResponse(
        job_id=str(job.id),
        status=job.status,
        product_count=job.product_count or 0,
    )


@router.get("/jobs/{job_id}", response_model=BatchJobStatusResponse, dependencies=[catalog_auth])
async def get_catalog_job_status(job_id: str, db: AsyncSession = Depends(get_db)):
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job id") from exc

    result = await db.execute(select(CatalogJob).where(CatalogJob.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    output_url = None
    if job.status == "completed" and job.output_file:
        output_url = f"/api/catalog/jobs/{job_id}/download"

    progress = _status_progress(job.status)

    return BatchJobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        product_count=job.product_count,
        product_title=job.product_title,
        output_file_url=output_url,
        error_message=job.error_message,
        progress=progress,
    )


@router.get("/jobs/{job_id}/download", dependencies=[catalog_auth])
async def download_catalog_pdf(job_id: str, db: AsyncSession = Depends(get_db)):
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job id") from exc

    result = await db.execute(select(CatalogJob).where(CatalogJob.id == job_uuid))
    job = result.scalar_one_or_none()
    if not job or job.status != "completed" or not job.output_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not available")

    pdf_path = Path(job.output_file)
    if not pdf_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF file missing")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=pdf_path.name,
    )


def _status_progress(status_value: str) -> int | None:
    mapping = {
        "received": 5,
        "validating": 10,
        "parsing": 20,
        "downloading_images": 40,
        "generating_content": 60,
        "rendering_pdf": 85,
        "completed": 100,
        "failed": 0,
    }
    return mapping.get(status_value)
