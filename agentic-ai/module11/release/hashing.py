# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Hashes: `sha256:<hex>` of normalized bytes; multi-file artifacts hash the
sorted lines "<relpath>\\0<filehash>\\n"; digests hash the canonical {artifactId: hash} map."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .normalize import canonical_json, normalize

PREFIX = "sha256:"


def sha256_bytes(data: bytes) -> str:
    return PREFIX + hashlib.sha256(data).hexdigest()


def file_hash(relpath: str, data: bytes) -> str:
    return sha256_bytes(normalize(relpath, data))


def files_hash(file_hashes: Mapping[str, str]) -> str:
    """Hash of a multi-file artifact from {relpath: filehash}."""
    lines = "".join(f"{path}\0{digest}\n" for path, digest in sorted(file_hashes.items()))
    return sha256_bytes(lines.encode("utf-8"))


def digest(hashes: Mapping[str, str]) -> str:
    return sha256_bytes(canonical_json(dict(hashes)))


def json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def hex_of(value: str) -> str:
    """`sha256:<hex>` -> `<hex>` (SSM names cannot contain ':')."""
    return value[len(PREFIX):] if value.startswith(PREFIX) else value
