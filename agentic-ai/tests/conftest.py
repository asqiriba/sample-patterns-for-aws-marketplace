# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/conftest.py
==================
Shared pytest fixtures for the tests/ suite.

Three autouse fixtures apply here:

- isolate_module10_export: for test_module10_* modules only, stops tests
  from loading real account settings or exporting telemetry to a configured
  collector (Logz.io / OTLP).
- isolate_module11: for test_module11* modules only, clears every M11_*
  environment variable and makes the module11.aws client factory (and
  boto3.client / boto3.Session.client) raise, so offline tests can never
  reach AWS. Live tests (test_module11_live_*) opt out with M11_LIVE_TESTS=1.
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


def _offline_client(*args, **kwargs):
    raise RuntimeError("offline test tried to build an AWS client")


@pytest.fixture(autouse=True)
def isolate_module11(monkeypatch, request):
    """Module 11 offline tests never read M11_* settings or build an AWS client."""
    name = request.module.__name__.split(".")[-1]
    if not name.startswith("test_module11"):
        return
    if name.startswith("test_module11_live") and os.environ.get("M11_LIVE_TESTS") == "1":
        return
    for key in [k for k in os.environ if k.startswith("M11_")]:
        monkeypatch.delenv(key, raising=False)
    import module11.aws
    monkeypatch.setattr(module11.aws, "client", _offline_client)
    try:
        import boto3
        import boto3.session
    except ImportError:
        return
    monkeypatch.setattr(boto3, "client", _offline_client)
    monkeypatch.setattr(boto3.session.Session, "client", _offline_client)


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
