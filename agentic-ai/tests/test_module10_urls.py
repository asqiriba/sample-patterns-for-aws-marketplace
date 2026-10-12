# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""module10.urls: HTTPS anywhere, plain HTTP only to loopback, named hosts or a configured collector,
and no other scheme ever reaches urllib."""
from __future__ import annotations

import urllib.request

import pytest

from module10 import urls


@pytest.mark.parametrize("url", ["https://api.example.com/invoke", "https://127.0.0.1:8443/ping",
                                 "http://127.0.0.1:4318", "http://localhost:8080/status", "http://[::1]:9000/x"])
def test_https_and_loopback_http_are_allowed(url):
    assert urls.check_url(url) == url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi",
                                 "custom://host/x", "http://example.com/x", "http://169.254.169.254/latest",
                                 "https:///no-host", "/relative/path", ""])
def test_other_schemes_and_remote_http_are_refused(url):
    with pytest.raises(ValueError, match="refusing"):
        urls.check_url(url)


def test_named_http_hosts_and_the_configured_collector():
    metadata = "http://169.254.170.2/v4/abc/task"
    with pytest.raises(ValueError):
        urls.check_url(metadata)
    assert urls.check_url(metadata, http_hosts={urls.ECS_METADATA_HOST}) == metadata
    assert urls.check_url("http://collector.internal:4318", any_http_host=True)
    with pytest.raises(ValueError):
        urls.check_url("file:///tmp/x", any_http_host=True)


def test_open_url_checks_before_urlopen_and_passes_the_request_through(monkeypatch):
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda target, **kw: seen.append((target, kw)) or "response")
    request = urllib.request.Request("https://api.example.com/invoke", data=b"{}")
    assert urls.open_url(request, timeout=5) == "response"
    assert seen == [(request, {"timeout": 5})]
    context = object()
    urls.open_url("https://127.0.0.1:8443/ping", timeout=1, context=context)
    assert seen[-1][1] == {"timeout": 1, "context": context}
    with pytest.raises(ValueError):
        urls.open_url(urllib.request.Request("file:///etc/passwd"), timeout=1)
    assert len(seen) == 2
