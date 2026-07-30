"""Concurrency-safe sequential internal lead number allocation.

Two independent safety layers protect against duplicate numbers:

1. A single-row-per-counter table (``lead_number_counters``) mutated inside
   a transaction holding a Postgres advisory lock (``pg_advisory_xact_lock``)
   plus a ``SELECT ... FOR UPDATE`` row lock, so two concurrent workers can
   never read-then-increment the same value at once.
2. A ``UNIQUE`` constraint on ``lead_processing_jobs.assigned_number`` as a
   hard backstop: even if the allocator above had a bug, the database would
   still reject a duplicate insert.

On SQLite (unit tests) advisory locks do not exist, so the in-process
``asyncio.Lock`` keeps the same call sequence safe for concurrency tests run
inside a single process; the unique-constraint backstop still works because
SQLite enforces ``UNIQUE`` too.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_processing_job import LeadNumberCounter

logger = logging.getLogger(__name__)

DEFAULT_COUNTER_NAME = "bbs_internal_lead_number"

_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}


def _local_lock(name: str) -> asyncio.Lock:
    lock = _LOCAL_LOCKS.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _LOCAL_LOCKS[name] = lock
    return lock


async def _advisory_lock(db: AsyncSession, name: str) -> None:
    bind = db.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name != "postgresql":
        return
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:name))"), {"name": name})


async def _get_or_create_counter(
    db: AsyncSession, *, name: str, floor_value: int
) -> LeadNumberCounter:
    result = await db.execute(
        select(LeadNumberCounter)
        .where(LeadNumberCounter.counter_name == name)
        .with_for_update()
    )
    counter = result.scalar_one_or_none()
    if counter is None:
        counter = LeadNumberCounter(
            counter_name=name, last_number=max(0, int(floor_value))
        )
        db.add(counter)
        await db.flush()
        return counter
    # The floor can only ever raise the counter (e.g. an operator manually
    # entered a higher number directly in Kommo/Sheets); it must never lower
    # it, or we would risk reissuing an already-used number.
    if floor_value > counter.last_number:
        counter.last_number = int(floor_value)
        await db.flush()
    return counter


async def allocate_next_number(
    db: AsyncSession,
    *,
    floor_hint: int = 0,
    counter_name: str = DEFAULT_COUNTER_NAME,
) -> str:
    """Allocate the next strictly-increasing internal number.

    Must be called inside an active transaction (``async with db.begin():``)
    so the advisory/row lock is held until commit.
    """
    async with _local_lock(counter_name):
        await _advisory_lock(db, counter_name)
        counter = await _get_or_create_counter(
            db, name=counter_name, floor_value=floor_hint
        )
        counter.last_number = int(counter.last_number) + 1
        await db.flush()
        logger.info(
            "lead_intake.numbering allocated=%s counter=%s",
            counter.last_number,
            counter_name,
        )
        return str(counter.last_number)


async def peek_next_number(
    db: AsyncSession,
    *,
    floor_hint: int = 0,
    counter_name: str = DEFAULT_COUNTER_NAME,
) -> str:
    """Read-only preview of the next number, without consuming it.

    Used for dry-run previews: the spec requires dry-run to *simulate* the
    proposed number rather than reserving it for real.
    """
    result = await db.execute(
        select(LeadNumberCounter).where(LeadNumberCounter.counter_name == counter_name)
    )
    counter = result.scalar_one_or_none()
    current = int(counter.last_number) if counter is not None else 0
    return str(max(current, int(floor_hint)) + 1)


async def assign_or_reuse_number(
    db: AsyncSession,
    *,
    existing_number: str | None,
    floor_hint: int = 0,
    dry_run: bool = False,
    counter_name: str = DEFAULT_COUNTER_NAME,
) -> tuple[str, bool]:
    """Reuse an existing sheet number, or allocate a fresh one.

    Returns ``(number, newly_allocated)``.
    """
    existing = str(existing_number or "").strip()
    if existing:
        return existing, False
    if dry_run:
        return await peek_next_number(db, floor_hint=floor_hint, counter_name=counter_name), True
    number = await allocate_next_number(db, floor_hint=floor_hint, counter_name=counter_name)
    return number, True
