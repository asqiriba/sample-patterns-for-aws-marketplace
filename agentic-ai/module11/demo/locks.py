# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Read the prep chain's stage locks, `module11/.local/locks/{dev,staging}.lock`.

The prep chain (`module11/.local/prep-chain.sh`) creates a lock under `set -o noclobber` in the subshell that
runs a step which deploys dev (`model compare`) or drives staging (`abtest run --stage staging`), writes
`{"step", "pid", "startedAt"}` into it, and removes it in that subshell's EXIT trap. This module only reads:
`demo arm --stage dev` refuses while `dev.lock` exists, and the demo driver's 2d skips the staging hotfix
while `staging.lock` exists. A lock whose pid is not running is reported as stale and never removed here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_DIR = Path(__file__).resolve().parents[1] / ".local" / "locks"


def lock_dir() -> Path:
    override = os.environ.get("M11_LOCK_DIR")
    return Path(override) if override else DEFAULT_DIR


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # exists, owned by someone else
        return True
    return True


@dataclass
class Lock:
    name: str
    path: Path
    step: str | None
    pid: int | None
    started_at: str | None
    alive: bool
    error: str | None = None

    @property
    def stale(self) -> bool:
        return not self.alive

    def message(self) -> str:
        who = f"{self.step or 'unknown step'} (pid {self.pid if self.pid is not None else '?'}, since " \
              f"{self.started_at or '?'})"
        if self.error:
            return f"{self.name}.lock is unreadable ({self.error}); check prep-chain.log, then delete the file"
        if self.stale:
            return f"stale {self.name}.lock from {self.step or 'unknown step'}; check prep-chain.log, then delete the file"
        return f"{self.name} is held by {who}"


def read(name: str, directory: Path | None = None, alive: Callable[[int], bool] = pid_alive) -> Lock | None:
    """The lock `<name>.lock`, or None when it does not exist. An unreadable lock counts as held and stale."""
    path = (directory or lock_dir()) / f"{name}.lock"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return Lock(name, path, None, None, None, alive=False, error=str(exc))
    try:
        doc = json.loads(text) if text.strip() else {}
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
        pid = doc.get("pid")
        pid = int(pid) if pid not in (None, "") else None
    except (ValueError, TypeError) as exc:
        return Lock(name, path, None, None, None, alive=False, error=str(exc))
    return Lock(name, path, doc.get("step"), pid, doc.get("startedAt"),
                alive=pid is not None and alive(pid))


def held(directory: Path | None = None, alive: Callable[[int], bool] = pid_alive) -> list[Lock]:
    """Every `*.lock` in the lock directory (preflight's quota-isolation item)."""
    root = directory or lock_dir()
    if not root.is_dir():
        return []
    return [lock for p in sorted(root.glob("*.lock")) if (lock := read(p.stem, root, alive)) is not None]
