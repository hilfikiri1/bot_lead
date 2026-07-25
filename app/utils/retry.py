from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps


def retry_async(
    max_attempts: int = 3,
    base_delay: float = 0.8,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable]], Callable[..., Awaitable]]:
    def decorator(func: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_on as exc:
                    last_error = exc
                    if attempt >= max_attempts:
                        raise
                    await asyncio.sleep(base_delay * attempt)
            if last_error:
                raise last_error

        return wrapper

    return decorator
