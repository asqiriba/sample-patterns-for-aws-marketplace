# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Objects by URI: `s3://<bucket>/<key>` in AWS, `file:///<dir>/<key>` for local runs.

The runtime and the tool handlers read evidence, snapshots, flags and bundle files and write
results and side effects through this one module. A `file://` URI
stands in for a bucket during `python -m module11.runtime.app` on 127.0.0.1: an object's
user metadata lives in a sidecar `<file>.metadata.json`, the way S3 keeps it beside the
object, so the `m11-sha256` check (M6) runs the same way locally.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

SHA_KEY = "m11-sha256"
META_SUFFIX = ".metadata.json"
_SERVICE = re.compile(r"^[a-z0-9-]{3,64}$")


class ObjectNotFound(LookupError):
    """No object at the URI (tool error code `not_found`)."""
    code = "not_found"


class DataMismatch(ValueError):
    """The object's `m11-sha256` metadata differs from the pinned hash (tool error `data_mismatch`, M6)."""
    code = "data_mismatch"


def join(base: str, key: str) -> str:
    """`join("s3://b/evidence/abc/", "incidents/x.json")`; also for bucket names and file dirs."""
    if "://" not in base:
        base = f"s3://{base}/"
    return base.rstrip("/") + "/" + key.lstrip("/")


def _split(uri: str) -> tuple[str, str, str]:
    parts = urlsplit(uri)
    if parts.scheme == "s3":
        if not parts.netloc or not parts.path.strip("/"):
            raise ValueError("s3 URI needs a bucket and a key")
        return "s3", parts.netloc, parts.path.lstrip("/")
    if parts.scheme == "file":
        return "file", "", unquote(parts.path)
    raise ValueError(f"unsupported object URI scheme: {parts.scheme or '(none)'}")


def _s3():
    # Read the factory at call time, so tests that replace module11.aws.client take effect.
    from . import client
    return client("s3")


def _not_found(exc: Exception) -> bool:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def head(uri: str) -> dict[str, str]:
    """User metadata of the object (lower-case keys). Raises ObjectNotFound."""
    scheme, bucket, key = _split(uri)
    if scheme == "file":
        path = Path(key)
        if not path.is_file():
            raise ObjectNotFound(uri)
        side = Path(key + META_SUFFIX)
        return json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
    try:
        response = _s3().head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # botocore ClientError; botocore is not imported offline
        if _not_found(exc):
            raise ObjectNotFound(uri) from exc
        raise
    return {k.lower(): v for k, v in (response.get("Metadata") or {}).items()}


def get(uri: str) -> tuple[bytes, str]:
    """(body, etag). The local etag is the body's sha256, which changes exactly when it does."""
    scheme, bucket, key = _split(uri)
    if scheme == "file":
        path = Path(key)
        if not path.is_file():
            raise ObjectNotFound(uri)
        body = path.read_bytes()
        return body, hashlib.sha256(body).hexdigest()
    try:
        response = _s3().get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if _not_found(exc):
            raise ObjectNotFound(uri) from exc
        raise
    return response["Body"].read(), str(response.get("ETag", "")).strip('"')


def put(uri: str, body: bytes, *, metadata: dict[str, str] | None = None,
        content_type: str = "application/json") -> None:
    scheme, bucket, key = _split(uri)
    if scheme == "file":
        path = Path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        if metadata:
            Path(key + META_SUFFIX).write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
        return
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key, "Body": body, "ContentType": content_type}
    if metadata:
        kwargs["Metadata"] = metadata
    _s3().put_object(**kwargs)


def _bare(value: str | None) -> str:
    value = (value or "").strip()
    return value[len("sha256:"):] if value.startswith("sha256:") else value


def read_incident(data: dict, service: str) -> tuple[dict, bytes]:
    """The service's evidence file under `context.data.evidence.uri`, hash-checked.
    Returns (parsed JSON, raw bytes). Raises ValueError, ObjectNotFound or DataMismatch."""
    if not isinstance(service, str) or not _SERVICE.match(service):
        raise ValueError("service_name must match ^[a-z0-9-]{3,64}$")
    ref = (data or {}).get("evidence") or {}
    if not ref.get("uri"):
        raise ValueError("context.data.evidence.uri is missing")
    body = verified_get(join(ref["uri"], f"incidents/{service}.json"), ref.get("sha256"))
    return json.loads(body), body


def verified_get(uri: str, expected_sha256: str | None) -> bytes:
    """HeadObject, compare `m11-sha256` with the pinned hash, then read.
    Raises DataMismatch when the hashes differ or either one is missing."""
    found = _bare(head(uri).get(SHA_KEY))
    wanted = _bare(expected_sha256)
    if not wanted or found != wanted:
        raise DataMismatch(f"{SHA_KEY} of {uri} does not match the pinned hash")
    body, _ = get(uri)
    return body
