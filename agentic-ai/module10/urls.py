# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one place Module 10 opens a URL: HTTPS, or plain HTTP only where the code needs it.

Plain HTTP is allowed to loopback (the local demo server, local Portkey, the local collector), to hosts a
caller names (the ECS task metadata endpoint), or, for an operator-configured OTLP collector whose documented
contract is an HTTP(S) base URL, to any host. Every other scheme (file:, ftp:, data:, custom) is refused
before urllib sees it.
"""
from __future__ import annotations

import ipaddress
from typing import Collection
from urllib.parse import urlsplit
import urllib.request

# ECS injects ECS_CONTAINER_METADATA_URI_V4 as http://169.254.170.2/v4/<id>: link-local, served by the agent.
ECS_METADATA_HOST = "169.254.170.2"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, *, http_hosts: Collection[str] = (), any_http_host: bool = False) -> str:
    """`url` unchanged if it may be opened, else ValueError (never echoes the URL: it can carry a token)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host and parts.scheme == "https":
        return url
    if host and parts.scheme == "http" and (any_http_host or is_loopback(host) or host in http_hosts):
        return url
    raise ValueError(f"refusing to open a {parts.scheme or 'scheme-less'} URL"
                     + (" to a non-loopback host" if parts.scheme == "http" else ""))


def open_url(target: str | urllib.request.Request, *, timeout: float, context=None,
             http_hosts: Collection[str] = (), any_http_host: bool = False):
    """urllib.request.urlopen after check_url; HTTPError and URLError propagate as before."""
    check_url(target.full_url if isinstance(target, urllib.request.Request) else target,
              http_hosts=http_hosts, any_http_host=any_http_host)
    kwargs = {"context": context} if context is not None else {}
    return urllib.request.urlopen(target, timeout=timeout, **kwargs)  # nosec B310 - scheme and host checked by check_url
