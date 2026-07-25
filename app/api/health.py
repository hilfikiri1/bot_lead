from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "babrik-1688-catalog-bot"}


@router.get("/")
async def root() -> dict[str, str]:
    return {"service": "babrik-1688-catalog-bot", "health": "/health"}
