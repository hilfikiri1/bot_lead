"""Shared test helpers for the lead-intake pipeline (not collected by pytest)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.lead_processing_job import LeadNumberCounter, LeadProcessingJob


async def make_engine_and_session_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """In-memory SQLite database with only the lead-intake tables created."""
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[LeadProcessingJob.__table__, LeadNumberCounter.__table__],
        )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


@asynccontextmanager
async def temp_db_session():
    engine, factory = await make_engine_and_session_factory()
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def make_row(
    *,
    row_number: int,
    phone: str | None = None,
    email: str | None = None,
    client_name: str | None = "Andrzej Janka",
    product: str | None = "narzędzia",
    lead_number: str | None = None,
    facebook_lead_id: str | None = None,
    region: str | None = "kujawsko-pomorskie",
    budget: str | None = "$5_000_-_$10_000",
    contact_channel: str | None = "whats_app",
):
    from app.services.google_sheets_service import SpreadsheetRow

    return SpreadsheetRow(
        row_number=row_number,
        phone=phone,
        email=email,
        client_name=client_name,
        company=None,
        product=product,
        lead_number=lead_number,
        region=region,
        budget=budget,
        contact_channel=contact_channel,
        facebook_lead_id=facebook_lead_id,
    )
