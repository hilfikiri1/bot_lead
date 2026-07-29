"""Safe request correlation for Railway logs."""
from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from uuid import uuid4

from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)
_REQUEST_ID: ContextVar[str | None] = ContextVar("bbs_request_id", default=None)
_INSTALLED = False


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def install_request_tracing(app: FastAPI) -> None:
    """Log start/end/error without request bodies, headers or secrets."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    @app.middleware("http")
    async def request_trace_middleware(request: Request, call_next):
        request_id = (
            request.headers.get("X-Request-ID")
            or request.headers.get("X-Correlation-ID")
            or f"req-{uuid4().hex[:12]}"
        )[:80]
        token = _REQUEST_ID.set(request_id)
        started = time.perf_counter()
        path = request.url.path
        method = request.method
        logger.info(
            "request_start request_id=%s method=%s path=%s content_length=%s",
            request_id,
            method,
            path,
            request.headers.get("content-length") or "-",
        )
        try:
            response = await call_next(request)
            duration_ms = int((time.perf_counter() - started) * 1000)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request_complete request_id=%s method=%s path=%s status=%s duration_ms=%s",
                request_id,
                method,
                path,
                response.status_code,
                duration_ms,
            )
            return response
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "request_failed request_id=%s method=%s path=%s duration_ms=%s error=%s",
                request_id,
                method,
                path,
                duration_ms,
                exc.__class__.__name__,
            )
            raise
        finally:
            _REQUEST_ID.reset(token)
