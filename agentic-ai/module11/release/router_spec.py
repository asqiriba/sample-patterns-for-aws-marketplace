# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The session router's reference implementation and the single definition of the rollout
enum and controller profiles.

`edge/lambda/router.mjs` embeds the constants generated from this file
(`python -m module11 gen router-consts` writes `module11/edge/lambda/router_consts.mjs`), and
both sides are tested against `tests/fixtures/module11/router_vectors.json`. Check C12 fails
when the generated constants are stale.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RolloutState(str, Enum):
    IDLE = "IDLE"
    SHADOWING = "SHADOWING"
    WARMING = "WARMING"
    OBSERVING = "OBSERVING"
    ADVANCING = "ADVANCING"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    PROMOTED = "PROMOTED"


STATES = tuple(s.value for s in RolloutState)
CANDIDATE_STATES = ("WARMING", "OBSERVING", "ADVANCING")
SHADOW_STATES = ("SHADOWING", "WARMING", "OBSERVING", "ADVANCING")
TERMINAL_STATES = ("PROMOTED", "ROLLED_BACK")
BUCKETS = 100
BUCKET_SEPARATOR = ":"
SHADOW_SEPARATOR = ":shadow:"
STATE_CACHE_SECONDS = 5
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
ROLLOUT_ID = re.compile(r"^r-[0-9a-f]{16}$")
SESSION_ID_MIN, SESSION_ID_MAX = 33, 256   #

CONSTS_PATH = "module11/edge/lambda/router_consts.mjs"
VECTORS_PATH = "tests/fixtures/module11/router_vectors.json"

# Latency and cost compare the arms only on sessions that end CANDIDATE_WARMUP_SECONDS or more after the step's
# first candidate session: a new runtime version (and every larger canary share) starts with cold capacity, and
# its first sessions wait for fresh microVMs. Measured over 11 dev canaries (sessions whose first streamed
# event took > 3 s): candidate 0.605 in its first minute, 0.50, 0.28 and 0.17 in the next three, 0.05 after
# 240 s; stable 0.00 in the same quiet minutes. Tool selection, errors, tool failures and quality count from the
# first session ("warm-up").
CANDIDATE_WARMUP_SECONDS = 240
# The controller profiles: the single definition. Changing a value changes the
# simulator's import closure, so `module11 sim verify` must be re-run (controller_sim_report.json).
PROFILES: dict[str, dict[str, Any]] = {
    "demo": {
        "label": "demo profile: compressed for a 5-minute stage beat",
        "steps": [50, 100], "shadowPct": 0, "warmupSessions": 6, "candidateWarmupSeconds": CANDIDATE_WARMUP_SECONDS, "consecutiveBreaches": 2,
        "cleanTicksToAdvance": 3, "dwellSeconds": 120,
        "window": {"mode": "sliding", "lookbackSeconds": 180, "minSamplesPerArm": 8, "minNewSamplesPerArm": 4},
        "alpha": 0.05, "minCandidateEvents": 2, "stallSeconds": 600,
        "requiredSignals": ["error_rate", "tool_selection", "tool_failure"], "qualityToPromote": False,
    },
    "prod": {
        "label": "prod profile",
        "steps": [5, 25, 50, 100], "shadowPct": 5, "warmupSessions": 20, "candidateWarmupSeconds": CANDIDATE_WARMUP_SECONDS, "consecutiveBreaches": 5,
        "cleanTicksToAdvance": 3, "dwellSeconds": 1800, "window": {"mode": "batch", "minSamplesPerArm": 50},
        "alpha": 0.10, "minCandidateEvents": 3, "stallSeconds": 14400,
        "rateRule": {"signals": ["error_rate", "tool_failure"], "alpha": 0.02, "maxLooks": 270,
                     "minNewCandidatePerLook": 5, "consecutive": 2},
        "requiredSignals": ["error_rate", "tool_selection", "tool_failure", "latency", "cost"],
        "qualityToPromote": True,
    },
    "model": {
        "label": "model profile",
        "shadowPhase": {"minHours": 24, "maxHours": 30},
        "steps": [5, 25, 50, 100], "shadowPct": 10, "warmupSessions": 20, "candidateWarmupSeconds": CANDIDATE_WARMUP_SECONDS, "consecutiveBreaches": 5,
        "cleanTicksToAdvance": 3, "dwellSeconds": 3600, "window": {"mode": "batch", "minSamplesPerArm": 50},
        "alpha": 0.10, "minCandidateEvents": 3, "stallSeconds": 14400,
        "rateRule": {"signals": ["error_rate", "tool_failure"], "alpha": 0.02, "maxLooks": 360,
                     "minNewCandidatePerLook": 5, "consecutive": 2},
        "requiredSignals": ["error_rate", "tool_selection", "tool_failure", "latency", "cost"],
        "qualityToPromote": True,
    },
}
QUALITY_MIN_PER_ARM = 200
WATCH_TOOLS = ("query_metrics", "search_runbooks", "get_service_logs")


# ---------------------------------------------------------------- hashing and routing
def _u64(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def bucket(rollout_id: str, conversation_id: str) -> int:
    return _u64(rollout_id + BUCKET_SEPARATOR + conversation_id) % BUCKETS


def shadow_bucket(rollout_id: str, conversation_id: str) -> int:
    return _u64(rollout_id + SHADOW_SEPARATOR + conversation_id) % BUCKETS


def route(state: Mapping[str, Any], conversation_id: str) -> str:
    """'candidate' iff the rollout is in a candidate state and the bucket is below canaryPct."""
    if state.get("state") in CANDIDATE_STATES and state.get("rolloutId") and \
            bucket(state["rolloutId"], conversation_id) < int(state.get("canaryPct") or 0):
        return "candidate"
    return "stable"


def shadow_enqueue(state: Mapping[str, Any], conversation_id: str) -> bool:
    """Shadow sampling uses its own hash input, so it is independent of the routing arm."""
    return bool(state.get("state") in SHADOW_STATES and state.get("rolloutId")
                and shadow_bucket(state["rolloutId"], conversation_id) < int(state.get("shadowPct") or 0))


def conversation_id(body: Mapping[str, Any], request_id: str) -> str:
    """The optional body field `conversation_id`, defaulting to the request ID."""
    value = body.get("conversation_id")
    if value is None:
        return request_id
    if not isinstance(value, str) or not CONVERSATION_ID.match(value):
        raise ValueError("conversation_id must match ^[A-Za-z0-9_-]{8,64}$")
    return value


def session_id(stagekey: str, qualifier: str, version: int | str, unique: str | None = None) -> str:
    """Fresh per request: m11-<stagekey>-<qualifier>-v<version>-<uuid4> (59 characters for prod)."""
    value = f"m11-{stagekey}-{qualifier}-v{int(version)}-{unique or uuid.uuid4()}"
    if not SESSION_ID_MIN <= len(value) <= SESSION_ID_MAX:
        raise ValueError(f"runtime session ID length {len(value)} outside {SESSION_ID_MIN}-{SESSION_ID_MAX}")
    return value


def opening(profile_name: str, shadow_pct: int | None = None) -> dict[str, Any]:
    """{state, canaryPct, shadowPct} when a rollout opens."""
    profile = PROFILES[profile_name]
    shadow = profile["shadowPct"] if shadow_pct is None else int(shadow_pct)
    if "shadowPhase" in profile:
        return {"state": "SHADOWING", "canaryPct": 0, "shadowPct": shadow}
    return {"state": "WARMING", "canaryPct": profile["steps"][0], "shadowPct": shadow}


def new_rollout_id() -> str:
    return "r-" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- generated JS constants
def js_constants() -> dict[str, Any]:
    return {
        "ROLLOUT_STATES": list(STATES), "CANDIDATE_STATES": list(CANDIDATE_STATES),
        "SHADOW_STATES": list(SHADOW_STATES), "BUCKETS": BUCKETS, "BUCKET_SEPARATOR": BUCKET_SEPARATOR,
        "SHADOW_SEPARATOR": SHADOW_SEPARATOR, "STATE_CACHE_MS": STATE_CACHE_SECONDS * 1000,
        "CONVERSATION_ID_PATTERN": CONVERSATION_ID.pattern, "SESSION_ID_MIN": SESSION_ID_MIN,
        "SESSION_ID_MAX": SESSION_ID_MAX,
    }


def render_js() -> str:
    consts = js_constants()
    spec = hashlib.sha256(json.dumps(consts, sort_keys=True).encode("utf-8")).hexdigest()
    lines = ["// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
             "// SPDX-License-Identifier: MIT-0",
             "// GENERATED by `python -m module11 gen router-consts` from module11/release/router_spec.py.",
             "// Do not edit: check C12 fails when this file differs from a regeneration.", ""]
    for name, value in consts.items():
        if name == "CONVERSATION_ID_PATTERN":
            lines.append(f"export const CONVERSATION_ID = new RegExp({json.dumps(value)});")
        elif isinstance(value, list):
            lines.append(f"export const {name} = Object.freeze({json.dumps(value)});")
        else:
            lines.append(f"export const {name} = {json.dumps(value)};")
    lines.append(f'export const SPEC_SHA256 = "{spec}";')
    return "\n".join(lines) + "\n"


VECTOR_ROLLOUTS = ("r-0123456789abcdef", "r-fedcba9876543210")
VECTOR_CASES = [(state, canary, shadow) for state in STATES
                for canary, shadow in ((50, 0), (5, 5), (25, 10), (50, 100), (100, 0))]


def vector_ids(count: int = 1000) -> list[str]:
    return [f"conv-{i:04d}-{hashlib.sha256(f'm11-vector-{i}'.encode()).hexdigest()[:20]}" for i in range(count)]


def vectors() -> dict[str, Any]:
    ids = vector_ids()
    out: dict[str, Any] = {"generatedBy": "python -m module11 gen router-consts", "ids": ids, "rollouts": {}}
    for rid in VECTOR_ROLLOUTS:
        cases = []
        for state, canary, shadow in VECTOR_CASES:
            item = {"rolloutId": rid, "state": state, "canaryPct": canary, "shadowPct": shadow}
            routes = "".join("1" if route(item, c) == "candidate" else "0" for c in ids)
            shadows = "".join("1" if shadow_enqueue(item, c) else "0" for c in ids)
            cases.append({"state": state, "canaryPct": canary, "shadowPct": shadow,
                          "candidate": routes.count("1"), "shadowed": shadows.count("1"),
                          "routeSha256": hashlib.sha256(routes.encode()).hexdigest(),
                          "shadowSha256": hashlib.sha256(shadows.encode()).hexdigest()})
        out["rollouts"][rid] = {"bucket": [bucket(rid, c) for c in ids],
                                "shadowBucket": [shadow_bucket(rid, c) for c in ids], "cases": cases}
    return out


def render_vectors() -> str:
    return json.dumps(vectors(), indent=1, sort_keys=True) + "\n"


def write_generated(root: str | Path) -> list[str]:
    """Write the JS constants and the shared vectors; return the paths that changed."""
    base = Path(root)
    changed = []
    for rel, text in ((CONSTS_PATH, render_js()), (VECTORS_PATH, render_vectors())):
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            changed.append(rel)
    return changed


def js_constants_current(root: str | Path) -> bool:
    """C12: the committed JS constants equal a regeneration. A tree without an edge/ directory
    (a fixture harness) has nothing to check."""
    base = Path(root)
    path = base / CONSTS_PATH
    if not path.is_file():
        return not (base / "module11/edge").exists()
    return path.read_text(encoding="utf-8") == render_js()
