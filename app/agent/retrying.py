"""Tenacity compatibility used by the agent modules.

Production installs tenacity from requirements.txt. The fallback keeps imports and
pure unit tests usable in stripped-down environments; it performs no retries.
"""
from __future__ import annotations

try:
    from tenacity import retry, stop_after_attempt, wait_exponential
except ImportError:  # pragma: no cover - only for minimal tooling environments
    def retry(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def stop_after_attempt(*args, **kwargs):
        return None

    def wait_exponential(*args, **kwargs):
        return None
