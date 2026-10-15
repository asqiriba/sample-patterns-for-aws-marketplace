# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Latched behavior digests. Pure.

A rollback for evidence (not a timeout) latches the candidate's behaviorDigest in SSM at
`/m11/devops-companion/latch/<hex>` (no ':' in the name). Seal and deploy refuse a latched digest
with `LatchedBehavior`; a new commit changes the digest. `module11 latch clear <hex> --reason`
writes an audit record.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .audit import iso
from .naming import latch_param


class LatchedBehavior(RuntimeError):
    def __init__(self, behavior_digest: str, record: dict) -> None:
        super().__init__(f"behaviorDigest {behavior_digest} is latched by rollout {record.get('rolloutId')} "
                         f"({record.get('reason')}); commit a fix or `module11 latch clear` with a reason")
        self.behavior_digest = behavior_digest
        self.record = record


def name(behavior_digest: str) -> str:
    return latch_param(behavior_digest)


def value(behavior_digest: str, rollout_id: str, reason: str, at: float) -> str:
    return json.dumps({"state": "latched", "behaviorDigest": behavior_digest, "rolloutId": rollout_id,
                       "reason": reason, "at": iso(at)}, sort_keys=True)


def check(behavior_digest: str, get_param: Callable[[str], str | None]) -> None:
    """Raise LatchedBehavior if the digest is latched. `get_param` returns None when absent."""
    raw = get_param(name(behavior_digest))
    if raw is None:
        return
    record: dict[str, Any] = json.loads(raw)
    if record.get("state") == "latched":
        raise LatchedBehavior(behavior_digest, record)


def cleared(behavior_digest: str, reason: str, by: str, at: float) -> tuple[str, dict]:
    """(new parameter value, audit record) for `module11 latch clear`."""
    if not reason.strip():
        raise ValueError("latch clear needs a reason")
    record = {"state": "cleared", "behaviorDigest": behavior_digest, "reason": reason, "by": by, "at": iso(at)}
    return json.dumps(record, sort_keys=True), {"action": "latch_clear", "actor": by, "details": record}
