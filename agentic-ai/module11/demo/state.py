# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11/.local/demo-state.json`: the one writer helper and its lock-free reader.

Every writer (the traffic driver, `demo arm`/`rearm`, `live_check --armed`, the prep chain, agents)
goes through `update(fn)`: an exclusive `fcntl.flock` on `demo-state.json.lock` (created if missing,
never deleted; the kernel drops it when the holder exits or crashes), read, apply `fn`, write a temp
file in the same directory, fsync it and `os.replace` it over the file. Readers call `read()` with no
lock: `os.replace` is atomic, so they see the old file or the new one, never a torn one.

A lock not obtained within 10 s, or an OS error, raises `DemoStateError` and leaves the file as it was.
Timestamps in the file are ISO 8601 UTC with an offset (`now_iso()`).
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".local" / "demo-state.json"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05


class DemoStateError(RuntimeError):
    pass


def path_of(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get("M11_DEMO_STATE")
    return Path(override) if override else DEFAULT_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(path: str | Path | None = None) -> dict:
    """The current document; a missing file is {}. Malformed JSON raises ValueError (callers decide)."""
    target = path_of(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{target} is not a JSON object")
    return doc


def _lock(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise DemoStateError(f"demo-state.json is locked by another writer for more than {timeout:.0f} s")
            time.sleep(LOCK_POLL_SECONDS)


def update(fn: Callable[[dict], dict], path: str | Path | None = None, *,
           timeout: float = LOCK_TIMEOUT_SECONDS) -> dict:
    """Apply `fn(doc) -> new doc` under the lock and replace the file atomically; returns the new doc."""
    target = path_of(path)
    lock_path = target.with_name(target.name + ".lock")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise DemoStateError(f"cannot open {lock_path.name}: {exc}") from exc
    try:
        _lock(fd, timeout)
        try:
            current = read(target)
        except (OSError, ValueError) as exc:
            raise DemoStateError(f"demo-state.json unreadable: {exc}") from exc
        new = fn(dict(current))
        if not isinstance(new, dict):
            raise DemoStateError("update function must return a dict")
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=".demo-state-",
                                             suffix=".tmp", delete=False) as tmp:
                tmp_name = tmp.name
                json.dump(new, tmp, indent=2, sort_keys=True, default=str)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
            tmp_name = None
        except OSError as exc:
            raise DemoStateError(f"demo-state.json write failed: {exc}") from exc
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return new
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def set_key(key: str, value: Any, path: str | Path | None = None) -> dict:
    """Replace one top-level key wholesale."""
    return update(lambda doc: {**doc, key: value}, path)
