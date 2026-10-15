# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""module11.aws.urls: the tooling's direct HTTP calls are HTTPS only; nothing else reaches urllib."""
from __future__ import annotations

import urllib.request

import pytest

from module11.aws import urls


def test_https_is_allowed():
    for url in ("https://abc123.execute-api.us-east-1.amazonaws.com/api/invoke", "https://gitlab.com/api/v4/x",
                "https://servicereference.us-east-1.amazonaws.com/v1/s3/s3.json"):
        assert urls.check_url(url) == url


@pytest.mark.parametrize("url", ["http://gitlab.com/api/v4", "http://127.0.0.1:8080/ping", "file:///etc/passwd",
                                 "ftp://example.com/x", "data:text/plain,hi", "https:///no-host", "/oauth2/authorize"])
def test_everything_else_is_refused(url):
    with pytest.raises(ValueError, match="non-HTTPS"):
        urls.check_url(url)


def test_open_url_checks_first(monkeypatch):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda target, timeout: seen.append(target) or "response")
    request = urllib.request.Request("https://api.example.com/api/invoke", method="OPTIONS")
    assert urls.open_url(request, timeout=30) == "response" and seen == [request]
    with pytest.raises(ValueError):
        urls.open_url(urllib.request.Request("file:///etc/passwd"), timeout=30)
    assert seen == [request]


def test_callers_use_the_helper():
    """The four direct callers open URLs only through urls.open_url."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("module11/aws/deploy.py", "module11/aws/ui_wiring.py", "module11/gitlab/seed.py",
                "module11/infrastructure/policy/refresh.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "urlopen(" not in text and "open_url(" in text, rel
