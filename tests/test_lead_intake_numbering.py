"""Concurrency-safe internal number allocation (mandatory cases 8, 9, 27)."""

from __future__ import annotations

import asyncio

import pytest

from app.services.lead_intake import numbering
from tests.lead_intake_helpers import make_engine_and_session_factory


@pytest.mark.asyncio
async def test_allocate_next_number_starts_from_floor_hint():
    engine, factory = await make_engine_and_session_factory()
    try:
        async with factory() as db:
            number = await numbering.allocate_next_number(db, floor_hint=166, counter_name="t1")
            await db.commit()
        assert number == "167"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_allocate_next_number_is_strictly_increasing():
    engine, factory = await make_engine_and_session_factory()
    try:
        allocated: list[str] = []
        for _ in range(5):
            async with factory() as db:
                allocated.append(await numbering.allocate_next_number(db, floor_hint=0, counter_name="t2"))
                await db.commit()
        assert allocated == ["1", "2", "3", "4", "5"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reuse_existing_number_does_not_consume_counter():
    engine, factory = await make_engine_and_session_factory()
    try:
        async with factory() as db:
            number, newly = await numbering.assign_or_reuse_number(
                db, existing_number="167", floor_hint=0, counter_name="t3"
            )
            await db.commit()
        assert number == "167"
        assert newly is False

        async with factory() as db:
            next_number = await numbering.allocate_next_number(db, floor_hint=0, counter_name="t3")
            await db.commit()
        # The counter never saw "167" go through it, so it starts fresh at 1.
        assert next_number == "1"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dry_run_peek_does_not_consume_counter():
    engine, factory = await make_engine_and_session_factory()
    try:
        async with factory() as db:
            first_peek = await numbering.peek_next_number(db, floor_hint=166, counter_name="t4")
            second_peek = await numbering.peek_next_number(db, floor_hint=166, counter_name="t4")
        assert first_peek == second_peek == "167"

        async with factory() as db:
            real = await numbering.allocate_next_number(db, floor_hint=166, counter_name="t4")
            await db.commit()
        assert real == "167"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_concurrent_workers_never_allocate_the_same_number():
    """Mandatory case 27: two workers cannot allocate the same internal number."""
    engine, factory = await make_engine_and_session_factory()
    try:
        async def worker() -> str:
            async with factory() as db:
                number = await numbering.allocate_next_number(db, floor_hint=0, counter_name="race")
                # Simulate other work happening before commit, to widen the
                # race window as much as possible for this test.
                await asyncio.sleep(0)
                await db.commit()
                return number

        results = await asyncio.gather(*(worker() for _ in range(20)))
        assert len(results) == len(set(results)) == 20
        assert sorted(int(value) for value in results) == list(range(1, 21))
    finally:
        await engine.dispose()
