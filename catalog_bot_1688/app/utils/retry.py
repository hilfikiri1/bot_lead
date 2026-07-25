"""Small async retry helper built on top of tenacity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


async def retry_async[T](
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    initial_wait: float = 1.0,
    max_wait: float = 10.0,
) -> T:
    """Run ``func`` with exponential backoff, retrying on the given exceptions.

    ``func`` must be a zero-argument coroutine factory (use ``functools.partial``
    or a lambda to bind arguments).
    """
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=initial_wait, max=max_wait),
        retry=retry_if_exception_type(exceptions),
        reraise=True,
    ):
        with attempt:
            return await func()
    raise RuntimeError("retry_async exhausted without returning")  # pragma: no cover
