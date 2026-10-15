# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Gate warm-up.

A gate runs right after `release deploy` creates a new runtime version, whose first sessions pay a cold start
(measured on harness/v2's staging gate: the first 12 sessions had a median of 33.8 s, later ones 11-14 s). The
baseline it is compared with was calibrated on prod `stable`, which is warm. So before the timed suite, the gate
sends `sessions` untimed warm-up sessions, one per worker slot, to the release's endpoint, and compares a warm
candidate with the warm baseline: the controller's warm-up rule applied to the gate. Thresholds and the
latency rule are unchanged.

Warm-up sessions have no side effects and pollute nothing:
- the envelope is `variant=eval, shadow=true`: the shadow copy of `record_incident_note` writes neither the note
  nor a Memory record; `eval` is excluded from every online-evaluation config;
- they call the runtime directly (no edge adapter), so no outcome or request items are written;
- they are tagged in their session ID (`m11-warmup-<runId>-<n>-<uuid8>`, the spans' `session.id`), request ID
  (`wu-...`) and `scenarioId` (`m11-warmup`), and stamped with the measured release's manifest version;
- they are never part of the RunRecord's sessions or results, so no gate statistic, coverage or
  cost-per-session includes them.

Nothing is hidden: the RunRecord's `warmup` block and the gate report list the session count and the cold-start
time to first event (median and max) of the new runtime version, informational and unscored.
"""
from __future__ import annotations

import os
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping

DEFAULT_SESSIONS = 12                  # one per invoke worker slot (runner.INVOKE_CONCURRENCY)
ENV = "M11_GATE_WARMUP"
SCENARIO_TAG = "m11-warmup"
SESSION_PREFIX = "m11-warmup"
WARMUP_INPUT = "The checkout service is throwing 502s since the 14:20 deploy. What changed?"


def sessions_from(value: int | str | None = None) -> int:
    """`--warmup N`, else M11_GATE_WARMUP, else 12; 0 turns it off. Bounded to 0..32."""
    raw = value if value is not None else os.environ.get(ENV, DEFAULT_SESSIONS)
    n = int(raw)
    if not 0 <= n <= 32:
        raise ValueError("warm-up sessions must be between 0 and 32")
    return n


def session_id(run_id: str, n: int) -> str:
    return f"{SESSION_PREFIX}-{run_id}-{int(n):02d}-{uuid.uuid4().hex[:8]}"


def envelope(target: Any, run_id: str, n: int) -> dict:
    """Side-effect-free (shadow) and tagged; the identity of the release being measured."""
    return {"identity": {"team": "platform", "principal": "m11-eval"},
            "request": {"change_summary": WARMUP_INPUT, "request_id": f"wu-{run_id}-{int(n):02d}"},
            "context": {"variant": "eval", "shadow": True, "scenarioId": SCENARIO_TAG,
                        "conversationId": f"warmup-{run_id}-{int(n):02d}", "manifestVersion": target.manifest_version,
                        "behaviorDigest": target.behavior_digest, "release": target.release}}


def _one(core: Any, target: Any, run_id: str, n: int, clock: Callable[[], float]) -> dict:
    import json
    sid = session_id(run_id, n)
    began = clock()
    row = {"sessionId": sid, "status": "invoke_error", "timeToFirstEventMs": None, "latencyMs": None}
    try:
        response = core.invoke_agent_runtime(agentRuntimeArn=target.runtime_arn, qualifier=target.endpoint,
                                             runtimeSessionId=sid, contentType="application/json",
                                             accept="text/event-stream",
                                             payload=json.dumps(envelope(target, run_id, n)).encode("utf-8"))
        stream = response["response"]
        first = stream.read(1)
        row["timeToFirstEventMs"] = round((clock() - began) * 1000.0, 1) if first else None
        rest = stream.read()
        row["latencyMs"] = round((clock() - began) * 1000.0, 1)
        from module11.release import deploy as D
        events = D.sse_events((first + rest).decode("utf-8", "replace"))
        final = next((data for name, data in reversed(events) if name in {"result", "error"}), None)
        row["status"] = str(final.get("status") or "failed") if isinstance(final, Mapping) else "no_result"
    except Exception as exc:  # noqa: BLE001 - a failed warm-up is reported, never fatal
        row["detail"] = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")) or type(exc).__name__
    try:
        core.stop_runtime_session(agentRuntimeArn=target.runtime_arn, qualifier=target.endpoint, runtimeSessionId=sid)
    except Exception:  # noqa: BLE001 - the 60 s idle timeout ends it anyway
        pass
    return row


def run(core: Any, target: Any, run_id: str, sessions: int, *, clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = print) -> dict:
    """Send `sessions` concurrent warm-up sessions and summarize them (the RunRecord's `warmup`)."""
    if sessions <= 0:
        return {"sessions": 0}
    t0 = clock()
    with ThreadPoolExecutor(max_workers=sessions, thread_name_prefix="m11-warmup") as pool:
        rows = list(pool.map(lambda n: _one(core, target, run_id, n, clock), range(1, sessions + 1)))
    out = summarize(rows)
    out.update(seconds=round(clock() - t0, 1), manifestVersion=target.manifest_version, endpoint=target.endpoint)
    log(f"warm-up: {out['sessions']} untimed sessions on {target.stage}/{target.endpoint} in {out['seconds']:.0f} s; "
        f"cold-start time to first event median {_s(out.get('timeToFirstEventMedianMs'))}, max "
        f"{_s(out.get('timeToFirstEventMaxMs'))} (informational, not gated)")
    return out


def summarize(rows: list[Mapping[str, Any]]) -> dict:
    ttfe = [r["timeToFirstEventMs"] for r in rows if r.get("timeToFirstEventMs") is not None]
    lat = [r["latencyMs"] for r in rows if r.get("latencyMs") is not None]
    out: dict[str, Any] = {"sessions": len(rows), "completed": sum(1 for r in rows if r.get("status") == "completed"),
                           "errors": sum(1 for r in rows if r.get("status") == "invoke_error"),
                           "sessionIds": [r["sessionId"] for r in rows]}
    if ttfe:
        out.update(timeToFirstEventMedianMs=round(statistics.median(ttfe), 1), timeToFirstEventMaxMs=max(ttfe))
    if lat:
        out.update(latencyMedianMs=round(statistics.median(lat), 1), latencyMaxMs=max(lat))
    return out


def _s(ms: float | None) -> str:
    return "n/a" if ms is None else f"{ms / 1000.0:.1f} s"


def markdown(block: Mapping[str, Any] | None) -> str:
    """The report's informational warm-up block (unscored)."""
    if not block or not block.get("sessions"):
        return "_Warm-up: none (cold start not measured)._"
    return (f"**Warm-up (informational, not scored):** {block['sessions']} untimed shadow session(s) on the new "
            f"runtime version before the suite; cold-start time to first event median "
            f"{_s(block.get('timeToFirstEventMedianMs'))}, max {_s(block.get('timeToFirstEventMaxMs'))}; session "
            f"latency median {_s(block.get('latencyMedianMs'))}, max {_s(block.get('latencyMaxMs'))}; "
            f"{block.get('completed', 0)} completed, {block.get('errors', 0)} invoke error(s). Gated latency is "
            "measured warm, like the calibrated baseline.")
