# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Flags-as-code kill switch. Pure edits plus an ETag retry loop over injected I/O.

`kill(doc, key, team)` removes the team from every variation-1 team target, adds it to the
variation-0 team target (created if absent) and bumps `version`; a second kill is a no-op.
`edit_with_retry(get, put, mutate)` is the read-modify-write with `IfMatch: <ETag>`, retried up
to 3 times on 412, the same concurrency rule as Module 10's control.mjs. The document stays in
Module 10's schema, so module10.flags.LocalLaunchDarkly evaluates it unchanged.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Callable, Mapping

from .hashing import file_hash

FLAGS_PATH = "module11/harness/flags/flags.json"
MAX_RETRIES = 3


class PreconditionFailed(Exception):
    """The object's ETag changed since it was read (HTTP 412)."""


class FlagEditConflict(RuntimeError):
    pass


def _team_targets(flag: dict, variation: int) -> list[dict]:
    return [t for t in flag.setdefault("contextTargets", []) if t.get("contextKind") == "team"
            and t.get("variation") == variation]


def kill(doc: Mapping[str, Any], key: str, team: str) -> tuple[dict, bool]:
    """(new document, changed)."""
    out = copy.deepcopy(dict(doc))
    flag = out["flags"][key]
    changed = False
    for target in _team_targets(flag, 1):
        if team in target.get("values", []):
            target["values"] = [v for v in target["values"] if v != team]
            changed = True
    off = _team_targets(flag, 0)
    if not off:
        off = [{"contextKind": "team", "values": [], "variation": 0}]
        flag["contextTargets"].insert(0, off[0])
    if team not in off[0]["values"]:
        off[0]["values"] = sorted([*off[0]["values"], team])
        changed = True
    flag["contextTargets"] = [t for t in flag["contextTargets"] if t.get("values") or t is off[0]]
    if changed:
        flag["version"] = int(flag.get("version", 0)) + 1
        return out, True
    return dict(doc), False


def restore_to(declared: Mapping[str, Any]) -> Callable[[dict], tuple[dict, bool]]:
    """`flags restore`: put back the declared document (the release's object version)."""
    def mutate(current: dict) -> tuple[dict, bool]:
        return copy.deepcopy(dict(declared)), current != declared
    return mutate


def flags_hash(doc: Mapping[str, Any]) -> str:
    """The normalized hash the manifest records as flags.sha256."""
    return file_hash(FLAGS_PATH, render(doc))


def render(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def edit_with_retry(get: Callable[[], tuple[dict, str]], put: Callable[[bytes, str], str],
                    mutate: Callable[[dict], tuple[dict, bool]]) -> tuple[dict, bool, int]:
    """Read (doc, etag), mutate, put with IfMatch; retry the whole cycle up to 3 times on 412.
    Returns (document, changed, attempts)."""
    for attempt in range(1, MAX_RETRIES + 2):
        doc, etag = get()
        new, changed = mutate(doc)
        if not changed:
            return doc, False, attempt
        try:
            put(render(new), etag)
            return new, True, attempt
        except PreconditionFailed:
            if attempt > MAX_RETRIES:
                break
    raise FlagEditConflict(f"flags object kept changing; gave up after {MAX_RETRIES + 1} attempts")


def audit_record(action: str, key: str, team: str | None, stage: str, before: Mapping, after: Mapping,
                 actor: str, at: str) -> dict:
    return {"action": f"flags_{action}", "actor": actor, "at": at, "decision": "applied",
            "details": {"flag": key, "team": team, "stage": stage, "before": flags_hash(before),
                        "after": flags_hash(after), "version": after["flags"][key].get("version")}}
