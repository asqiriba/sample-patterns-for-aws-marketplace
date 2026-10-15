# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The staging config-bundle A/B test through Gateway `m11-staging-ab` (6.5 T-3 d; F4 `abtest show`).

`module11 abtest run --stage staging` (operator):
1. Control: the bundle version staging serves (`/current.deployment.staging.bundle`). Treatment: a new version on
   staging's bundle, branch `abtest`, whose companion prompt is a recommendation's (`--recommendation <id>`) or a
   given file (`--prompt-file`); every other component field is the control's. The version is written to the
   release ledger (`kind=bundle-version`, purpose `abtest`), so the drift checker does not report it.
2. Gateway tracing: a CloudWatch vended-logs delivery of the Gateway's TRACES to X-Ray (`aws/spans`), so each
   scored session can be attributed to its variant; created once, ledgered (`kind=gateway-tracing`).
3. `CreateABTest` (Preview): variants `C` and `T1` at 50/50, scored by staging's online-evaluation config,
   with the execution role `m11-staging-abtest`; ledgered (`kind=abtest`).
4. About 200 sessions through the Gateway's target URL as `m11-staging-abtest-driver` (the only identity allowed to
   invoke it), envelope `variant=staging` with staging's release identity, the S7 fixture mix, 6 at a time.
`module11 abtest show [<id>]`: per evaluator, control and treatment mean and n, the absolute change, p-value and
significance (`GetABTest.results`); online evaluation scores sessions a few minutes after they end.
Graceful fallback (Preview): if the service refuses the A/B API, the command says so and exits 3; the documented
alternative is a target-based A/B with two staging endpoints, or comparing the two variants offline with
`module11 gate --scope full` against each bundle.
"""
from __future__ import annotations

import copy
import json
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from module11.release.hashing import sha256_bytes

AB_NAME = re.compile(r"^m11_staging_ab_[0-9a-f]{8}$")
BRANCH = "abtest"
DEFAULT_SESSIONS = 200
DEFAULT_CONCURRENCY = 6
TRACES_SOURCE = "m11-staging-ab-traces"
TRACES_DESTINATION = "m11-staging-ab-xray"


class ABTestUnavailable(RuntimeError):
    """The Preview A/B API refused the request: the fallback applies (exit 3)."""


def new_name() -> str:
    return f"m11_staging_ab_{uuid.uuid4().hex[:8]}"


def treatment_component(control: Mapping[str, Any], prompt: str, agent: str = "companion") -> dict:
    """The control component with one agent's prompt replaced (normalized line endings, one final newline)."""
    if agent not in (control.get("prompts") or {}):
        raise ValueError(f"the control component has no {agent} prompt")
    text = prompt.replace("\r\n", "\n").strip() + "\n"
    if not text.strip():
        raise ValueError("the treatment prompt is empty")
    out = copy.deepcopy(dict(control))
    out["prompts"][agent] = text
    return out


def component_sha256(component: Mapping[str, Any]) -> str:
    return sha256_bytes(json.dumps(component, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def variants(bundle_arn: str, control_version: str, treatment_version: str, weights: tuple[int, int] = (50, 50)) -> list:
    if sum(weights) != 100 or min(weights) <= 0:
        raise ValueError("weights must be two positive integers adding to 100")
    return [{"name": "C", "weight": weights[0], "variantConfiguration": {"configurationBundle": {
                "bundleArn": bundle_arn, "bundleVersion": control_version}}},
            {"name": "T1", "weight": weights[1], "variantConfiguration": {"configurationBundle": {
                "bundleArn": bundle_arn, "bundleVersion": treatment_version}}}]


def target_url(gateway_url: str, target: str) -> str:
    """The HTTP runtime target's base URL; `invoke_agent_runtime` appends `/runtimes/.../invocations`."""
    return f"{gateway_url.rstrip('/')}/{target}"


@dataclass
class Row:
    evaluator: str
    control_n: int
    control_mean: float | None
    treatment_n: int
    treatment_mean: float | None
    delta: float | None
    p_value: float | None
    significant: bool


def result_rows(response: Mapping[str, Any]) -> list[Row]:
    """Per-evaluator rows from GetABTest's `results` (treatment = the first non-control variant)."""
    out = []
    for metric in ((response.get("results") or {}).get("evaluatorMetrics") or []):
        control = metric.get("controlStats") or {}
        treat = next(iter(metric.get("variantResults") or []), {})
        out.append(Row(str(metric.get("evaluatorArn", "")).rsplit("/", 1)[-1], int(control.get("sampleSize") or 0),
                       control.get("mean"), int(treat.get("sampleSize") or 0), treat.get("mean"),
                       treat.get("absoluteChange"), treat.get("pValue"), bool(treat.get("isSignificant"))))
    return out


def show_lines(response: Mapping[str, Any], record: Mapping[str, Any] | None = None) -> list[str]:
    rec = record or {}
    head = (f"A/B test {response.get('name')} ({response.get('abTestId')}): {response.get('status')}/"
            f"{response.get('executionStatus')}, Gateway {rec.get('gateway', 'm11-staging-ab')}, config bundles "
            f"C={rec.get('controlVersion', '?')[:8]} T1={rec.get('treatmentVersion', '?')[:8]}")
    out = [head]
    if rec.get("treatmentSource"):
        out.append(f"  treatment: {rec['treatmentSource']}")
    if rec.get("sessions"):
        out.append(f"  traffic: {rec['sessions'].get('sent', 0)} sessions sent through the Gateway "
                   f"({rec['sessions'].get('failed', 0)} failed), scored by online evaluation")
    rows = result_rows(response)
    if not rows:
        out.append("  no results yet: online evaluation scores sessions a few minutes after they end")
        return out
    out.append(f"  {'evaluator':<26} {'C mean (n)':>14} {'T1 mean (n)':>14} {'delta':>8} {'p-value':>9} significant")
    for r in rows:
        c = f"{r.control_mean:.3f} ({r.control_n})" if r.control_mean is not None else f"- ({r.control_n})"
        t = f"{r.treatment_mean:.3f} ({r.treatment_n})" if r.treatment_mean is not None else f"- ({r.treatment_n})"
        d = f"{r.delta:+.3f}" if r.delta is not None else "-"
        p = f"{r.p_value:.3g}" if r.p_value is not None else "-"
        out.append(f"  {r.evaluator:<26} {c:>14} {t:>14} {d:>8} {p:>9} {'yes' if r.significant else 'no'}")
    out.append("  significance: p < 0.05 (service-computed); the decision still goes through the gate as an MR")
    return out


# ---------------------------------------------------------------- the traffic (Gateway, SigV4)
@dataclass
class Traffic:
    sent: int = 0
    completed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def envelope(team: str, text: str, identity: Mapping[str, str]) -> dict:
    return {"identity": {"team": team, "principal": "m11-abtest-driver"},
            "request": {"change_summary": text, "request_id": f"ab-{uuid.uuid4().hex[:12]}"},
            "context": {"variant": "staging", "manifestVersion": identity["manifestVersion"],
                        "behaviorDigest": identity["behaviorDigest"], "release": identity["release"]}}


def drive(invoke: Callable[[str, bytes], bytes], *, sessions: int, concurrency: int, identity: Mapping[str, str],
          fixtures: Mapping[str, Sequence[str]], mix: Mapping[str, float], ab_name: str,
          log: Callable[[str], None] = print, seed: int | None = None) -> Traffic:
    """Send `sessions` sessions (new runtime session IDs, so the Gateway splits them) with `concurrency` workers."""
    rng = random.Random(seed)
    teams = list(mix)
    jobs = [rng.choices(teams, weights=[mix[t] for t in teams])[0] for _ in range(sessions)]
    lock, out = threading.Lock(), Traffic()
    work = list(enumerate(jobs))

    def worker() -> None:
        while True:
            with lock:
                if not work:
                    return
                i, team = work.pop(0)
            text = rng.choice(list(fixtures[team]))
            sid = f"m11-abtest-{ab_name[-8:]}-{uuid.uuid4().hex}"
            try:
                body = invoke(sid, json.dumps(envelope(team, text, identity)).encode("utf-8"))
                ok = b"event: result" in body             # the runtime's final event; `event: error` otherwise
            except Exception as exc:  # counted, never re-sent (no proof it did not reach the runtime)
                ok = False
                with lock:
                    out.errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            with lock:
                out.sent += 1
                out.completed += ok
                out.failed += not ok
                if out.sent % 25 == 0:
                    log(f"  {out.sent}/{sessions} sessions sent ({out.failed} failed)")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def wait_status(get: Callable[[], Mapping[str, Any]], want: Sequence[str], *, timeout: float = 300,
                sleep: Callable[[float], None] = time.sleep) -> Mapping[str, Any]:
    end = time.monotonic() + timeout
    while True:
        doc = get()
        if doc.get("status") in want:
            return doc
        if str(doc.get("status", "")).endswith("FAILED"):
            raise ABTestUnavailable(f"A/B test {doc.get('status')}: {doc.get('errorDetails')}")
        if time.monotonic() > end:
            raise TimeoutError(f"A/B test still {doc.get('status')}")
        sleep(5)
