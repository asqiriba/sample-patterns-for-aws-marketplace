# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Thin AWS SDK adapters for Module 11.

Every boto3 client Module 11 builds comes from `client()`, so offline tests
can replace this one factory (tests/conftest.py `isolate_module11`) and any
accidental AWS call fails loudly instead of reaching an account. The release
engines under `module11/release/` never import this package.
"""
from __future__ import annotations

from typing import Any


def client(service_name: str, **kwargs: Any) -> Any:
    """Return a boto3 client for `service_name` (region and profile from the standard chain)."""
    import boto3

    return boto3.client(service_name, **kwargs)
