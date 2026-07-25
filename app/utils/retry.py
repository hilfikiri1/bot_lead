from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def async_retry(func: Callable[[], Awaitable[T]], *, attempts: int, retry_exceptions: tuple[type[BaseException], ...], base_delay: float = 0.8) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except retry_exceptions as exc:
            last_error = exc
            if attempt == attempts:
                break
            await asyncio.sleep(base_delay * attempt)
    assert last_error is not None
    raise last_error
