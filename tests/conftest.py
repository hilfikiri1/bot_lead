"""
Shared pytest configuration for the catalog bot test suite.
Sets asyncio_fixture_loop_scope to suppress the deprecation warning.
"""
import pytest


# Suppress pytest-asyncio loop scope warning
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )
