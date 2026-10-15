# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Flags as code: Module 10's `LocalLaunchDarkly` over the stage's flags object.

The flags document lives at `<artifactsBucket>/<flagsKey>` (written by `release deploy` from
`harness/flags/flags.json`). Each invocation reads the object; a client is built once per ETag
and reused until the object changes, so a kill switch takes effect on the next request without a
runtime version. `m11.flags_hash` is the sha256 of the exact document and `m11.flags_resolved`
the evaluated values, so every session records which flag object it ran under.
"""
from __future__ import annotations

import hashlib
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from module10.flags import FLAG_KEY, LocalLaunchDarkly

from module11.aws import objects


class FlagsUnavailable(RuntimeError):
    """The flags object could not be read or evaluated; the invocation fails (no default-on)."""


class FlagSource:
    def __init__(self, uri: str, read: Callable[[str], tuple[bytes, str]] | None = None,
                 client_factory: Callable[[Path], Any] = LocalLaunchDarkly) -> None:
        self.uri = uri
        self._read = read or objects.get
        self._factory = client_factory
        self._lock = threading.Lock()
        self._etag: str | None = None
        self._client: Any = None
        self._hash: str | None = None
        self._dir = tempfile.TemporaryDirectory(prefix="m11-flags-")
        self.builds = 0

    def _current(self) -> tuple[Any, str]:
        try:
            body, etag = self._read(self.uri)
        except Exception as exc:
            if self._client is not None:  # keep serving the last good document on a read failure
                return self._client, self._hash
            raise FlagsUnavailable(type(exc).__name__) from exc
        with self._lock:
            if etag != self._etag or self._client is None:
                path = Path(self._dir.name) / f"flags-{hashlib.sha256(etag.encode()).hexdigest()[:16]}.json"
                path.write_bytes(body)
                try:
                    client = self._factory(path)
                except Exception as exc:
                    raise FlagsUnavailable(type(exc).__name__) from exc
                old, self._client = self._client, client
                self._etag, self._hash = etag, "sha256:" + hashlib.sha256(body).hexdigest()
                self.builds += 1
                if old is not None and hasattr(old, "close"):
                    old.close()
            return self._client, self._hash

    def evaluate(self, team: str) -> tuple[dict, str, dict]:
        """(Module 10 release record, flags hash, {flag key: value})."""
        client, flags_hash = self._current()
        try:
            release = client.evaluate(team)
        except Exception as exc:
            raise FlagsUnavailable(type(exc).__name__) from exc
        return release, flags_hash, {FLAG_KEY: release["recovery_planning_enabled"]}

    def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()
        self._dir.cleanup()


def flag_enabled(resolved: dict, key: str | None) -> bool:
    """A tool with `requiresFlag` is offered only when that flag evaluated true (fail closed)."""
    return key is None or resolved.get(key) is True
