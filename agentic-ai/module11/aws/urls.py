# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The tooling's direct HTTP calls (outside the GitLab and Vercel clients): HTTPS only.

Every URL these callers open is HTTPS: a stage's API Gateway URL, the Vercel branch alias and the Cognito
hosted domain, the GitLab API, the AWS Service Reference. Anything else (http:, file:, custom schemes) is
refused before urllib sees it.
"""
from __future__ import annotations

import urllib.request
from urllib.parse import urlsplit


def check_url(url: str) -> str:
    """`url` unchanged if it is https with a host, else ValueError (the URL is not echoed: it can carry a query)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"refusing to open a non-HTTPS URL (scheme {parts.scheme or 'none'!r})")
    return url


def open_url(target: str | urllib.request.Request, *, timeout: float):
    """urllib.request.urlopen after check_url; HTTPError and URLError propagate as before."""
    check_url(target.full_url if isinstance(target, urllib.request.Request) else target)
    return urllib.request.urlopen(target, timeout=timeout)  # nosec B310 - https only, checked by check_url
