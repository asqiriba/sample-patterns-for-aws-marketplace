# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/conftest.py
==================
Shared pytest fixtures for the tests/ suite.

Two autouse fixtures apply here:

- isolate_module10_export: for test_module10_* modules only, stops tests
  from loading real account settings or exporting telemetry to a configured
  collector (Logz.io / OTLP).
- mock_memory_env: for every test, enforces AGENT_MOCK_MEMORY=true and
  clears the Module 7 _get_stores() lru_cache before and after the test so
  memory store state does not leak between tests.

The agentic-ai/ root is put on sys.path so that a bare `pytest tests/...`
(the documented form) imports the module packages the same way
`python -m pytest` does.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True)
def isolate_module10_export(monkeypatch, request):
    """Tests never load real account settings or contact a configured collector."""
    if request.module.__name__.split(".")[-1].startswith("test_module10"):
        from module10 import observability
        monkeypatch.setattr(observability, "load_environment", lambda *args, **kwargs: None)
        monkeypatch.setenv("MODULE10_OTLP_ENDPOINT", "")
        for key in ("LOGZIO_REGION", "LOGZIO_LOGS_TOKEN", "LOGZIO_TRACES_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv("MODULE10_DEMO_SESSION", raising=False)


@pytest.fixture(autouse=True)
def mock_memory_env(monkeypatch):
    """Enforce mock mode and reset store cache for every test."""
    monkeypatch.setenv("AGENT_MOCK_MEMORY", "true")

    # Clear lru_cache before test so each test gets fresh store instances
    try:
        from module7.tools.memory_tools import _get_stores
        _get_stores.cache_clear()
    except ImportError:
        pass

    yield

    # Clear again after test to avoid state leaking to the next test
    try:
        from module7.tools.memory_tools import _get_stores
        _get_stores.cache_clear()
    except ImportError:
        pass
