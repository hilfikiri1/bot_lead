"""Test the same final compatibility layer that production installs last."""
from __future__ import annotations


def pytest_runtest_setup(item) -> None:  # noqa: ARG001
    from app.services.final_compat_runtime import install_final_compat_runtime

    install_final_compat_runtime()
