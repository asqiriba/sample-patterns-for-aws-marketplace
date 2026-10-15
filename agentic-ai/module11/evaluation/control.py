# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The gate's concurrent control arm and relative latency rule.

An absolute p95 budget, calibrated in a healthy window, failed good releases whenever the model provider's
tail latency rose (2026-10-07: 5 of 7 gates, model p99 15-22 s against 4-7 s when they passed). So latency is
judged relative to the release that serves prod `stable`, measured in the same window:

- **A balanced phase.** After the main suite's invocations, the run's happy_path scenarios run CONTROL_REPEATS
  times on both arms *at the same time*. The candidate arm (20 x 2 = 40 sessions) runs on the stage under test. The
  control arm (40 sessions) runs on prod's `stable` endpoint as the prod eval runner, with a session policy that
  allows only InvokeAgentRuntime and StopRuntimeSession on prod's runtime. Each arm has its own pool of
  CONTROL_CONCURRENCY workers, so both runtimes carry the same load in the same window and meet the same provider.
  CONTROL_WARMUP (one per control worker) untimed warm-up sessions go to `stable` first; the candidate is
  warm from the main suite.
  - Equal load is the point. A first version interleaved 40 control jobs into the main suite's 12 slots. That put
    about 9 concurrent sessions on the staging runtime against about 3 on prod's, and session latency follows
    per-runtime concurrency: on 2026-10-08 the control median was 6.7 s, the candidate's 12 s, and prod `stable`
    itself at 12 concurrent 11.4-13.2 s. An unchanged docs-only release (harness/v9, run 29379bf72703) failed that
    way.
- **Side effects.** Both arms use the gate's own envelope (`variant=eval, shadow=true`), so the shadow copy of
  `record_incident_note` writes no note and no Memory record. `eval` matches no online-evaluation filter, and
  direct runtime calls write no outcome or request item. Session IDs start `m11-control-<runId>-` (control) or
  `m11-latency-<runId>-` (candidate); request IDs start `ct-` or `lt-`. Neither is in the main suite's span query
  (`m11-eval-<runId>-`). Phase sessions are never evaluated and are in no statistic, coverage count or cost per
  session. The report shows their measured cost separately.
- **Rule.** The controller's own latency signal, `signals.latency`. The candidate arm's sessions breach
  when their p95 is above 1.25 x the control's p95, *and* the one-sided exact exceedance test (`stats.
  exceedance_greater`: how many candidate sessions exceed the control's order-statistic p95, m = ceil(0.95 n))
  gives p < ALPHA. This is the test the canary uses, so the gate and the canary judge latency the same way. The
  gate makes one look per release, so ALPHA is 0.01, not the canary's per-tick 0.05.
  - Sized by resampling the 2026-10-07 gate runs (4,000 trials each, both arms drawn from one run's happy_path
    sessions). At 40 vs 40, a release that is not slower fails in 0.35% of healthy windows and 0.38% of degraded
    ones. A 1.5x slowdown is caught 68% and 35% of the time; a 2x slowdown 96% and 78%. The phase adds 80
    sessions, about $4 and a minute per gate.
- **Cache warmth.** `signals.latency` refuses to judge until the candidate's prompt-cache ratio is within 0.2 of
  stable's. Both gate arms are warmed first, so the gate passes equal markers instead of measured ratios.

The absolute p95 budget stays in the report as an informational "user-experience budget" line. Token and
tool-call budgets and the hard gates are unchanged.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from module11.release import naming

CONTROL_STAGE, CONTROL_ENDPOINT, CONTROL_SCOPE = "prod", "stable", "happy_path"
CONTROL_REPEATS = 2
CONTROL_CONCURRENCY = 12                         # per arm, the main suite's concurrency
# One warm-up session per control worker, as the candidate gets 12 before its suite: with 6, the control's first
# round met cold capacity (2026-10-08, run c8284e167dd2: 3 of its first 12 sessions at 27-31 s, then medians equal).
CONTROL_WARMUP = CONTROL_CONCURRENCY
ALPHA = 0.01
MIN_SESSIONS = 20
EFFECT = 1.25                                    # signals.latency's fixed effect threshold
SESSION_PREFIX = {"control": "m11-control", "candidate": "m11-latency"}
REQUEST_PREFIX = {"control": "ct", "candidate": "lt"}


@dataclass(frozen=True)
class ControlArm:
    clients: Any                     # runner.Clients for the prod eval runner (session policy applied)
    target: Any                      # runner.Target: prod `stable`
    scenarios: Sequence[Mapping[str, Any]]
    repeats: int = CONTROL_REPEATS
    warmup: int = CONTROL_WARMUP
    concurrency: int = CONTROL_CONCURRENCY


def scenarios_for(chosen: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The control arm's scenarios: the run's happy_path ones (none in a smoke or tagged scope)."""
    return [s for s in chosen if s.get("category") == CONTROL_SCOPE]


ARM_SESSIONS = 40                                # per arm: the size the false-fail and power figures above assume


def repeats_for(scenarios: Sequence[Mapping[str, Any]]) -> int:
    """Repeats per scenario so each arm has ARM_SESSIONS sessions. A full scope's 20 happy_path scenarios keep
    CONTROL_REPEATS (40); the MR gate's `tag:fast` scope has 4, which at 2 repeats gave 8 vs 8, below MIN_SESSIONS,
    so every MR gate was INCONCLUSIVE (found by the first MR gate). Sample size only: the rule, ALPHA,
    EFFECT and MIN_SESSIONS are unchanged."""
    if not scenarios:
        return CONTROL_REPEATS
    return max(CONTROL_REPEATS, math.ceil(ARM_SESSIONS / len(scenarios)))


def session_id(run_id: str, scenario: str, repeat: int, suffix: str, arm: str = "control") -> str:
    value = f"{SESSION_PREFIX[arm]}-{run_id}-{scenario}-{int(repeat)}-{suffix}"
    if not 33 <= len(value) <= 256:
        raise naming.NameError_(f"runtime session ID of {len(value)} characters is outside 33..256")
    return value


def session_policy(runtime_arn: str) -> str:
    """The control client's session policy: invoke and stop sessions on prod's runtime, nothing else."""
    return json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "ControlArmInvokeOnly", "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
        "Resource": [runtime_arn, f"{runtime_arn}/*"]}]}, sort_keys=True)


def latencies(sessions: Iterable[Mapping[str, Any]], category: str = CONTROL_SCOPE) -> list[float]:
    """The sessions the rule compares: the category's sessions that reached the runtime (as the budget metrics
    count them), both arms filtered the same way."""
    return [float(s["latencyMs"]) for s in sessions
            if s.get("category") == category and s.get("status") != "invoke_error" and s.get("latencyMs")]


def p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]


def judge(candidate: Sequence[float], control: Sequence[float], alpha: float = ALPHA,
          min_n: int = MIN_SESSIONS) -> dict:
    """`signals.latency` on the two arms: {status: PASS|FAIL|INCONCLUSIVE, p95, controlP95, n, controlN, detail}."""
    from module11.release import signals as S
    marker = {"cache_n": 1, "cache_sum": 1.0}      # both arms warmed (module docstring)
    result = S.latency(S.ArmAgg(n=len(candidate), latencies=list(candidate), **marker),
                       S.ArmAgg(n=len(control), latencies=list(control), **marker), alpha, min_n)
    status = {S.OK: "PASS", S.BREACH: "FAIL", S.INSUFFICIENT: "INCONCLUSIVE"}[result.status]
    return {"status": status, "p95Ms": p95(candidate), "controlP95Ms": p95(control), "n": len(candidate),
            "controlN": len(control), "alpha": alpha, "minSessions": min_n, "detail": result.detail or ""}


def markdown(block: Mapping[str, Any] | None) -> str:
    head = "**Latency control phase:**"
    if not block or not block.get("sessions"):
        return f"{head} not run ({(block or {}).get('reason') or 'no happy_path scenarios in scope'})."
    target = block.get("target") or {}
    warm = block.get("warmup") or {}
    workers = (block.get("concurrency") or {}).get("control", CONTROL_CONCURRENCY)
    return (f"{head} {block.get('candidateSessions', 0)} candidate and {block['sessions']} control happy_path "
            f"session(s) at the same time, {workers} workers per arm; control = {target.get('release')} on prod "
            f"`{target.get('endpoint')}` ({target.get('manifestVersion')}) after {warm.get('sessions', 0)} warm-up "
            f"session(s); {float(block.get('seconds') or 0):.0f} s. Shadow, never evaluated, in no other statistic; "
            f"measured cost ${float(block.get('costUsd') or 0):.2f}. The p95 latency gate compares the two arms "
            f"(`signals.latency`, effect > {EFFECT}x and exact exceedance p < {ALPHA}); the main suite's p95 is the "
            f"informational user-experience line.")
