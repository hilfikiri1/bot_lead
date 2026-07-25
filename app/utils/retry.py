"""Retry utilities."""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def async_retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    retryable: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            current_delay = delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
