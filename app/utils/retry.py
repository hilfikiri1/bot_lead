from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Type


def async_retry(
    max_attempts: int = 3,
    backoff: float = 2.0,
    exceptions: tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """
    Decorator for async functions that retries on specified exceptions
    with exponential back-off.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts:
                        wait = backoff * (2 ** (attempt - 1))
                        await asyncio.sleep(wait)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator
