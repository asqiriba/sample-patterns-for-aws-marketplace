# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Seeded rollout simulator: drives the pure `controller.decide()` with synthetic sessions
("Tests of the rules"; the implementation-grade successor of the scratch models).

Shared assumptions: per-arm error and tool-failure rates 2% unless a case names another base;
watched tools selected at 0.95, 0.95 and 0.60; judge scores with mean 0.80 and SD 0.25 arriving
10-30 minutes after a session; candidate sessions sampled for online evaluation at 100% and
stable at 10%; a random cron phase; endpoint READY 30-75 s after a retarget.

- Demo cases replay the S7 traffic shape (per-team pools of 12 payments and 8 platform workers,
  S7_SESSIONS sessions, durations from tests/fixtures/module11/session_durations.json) or Poisson
  traffic until a terminal state, through `signals.SessionLog` and the real poll window.
- Batch cases (prod, model) use Poisson traffic aggregated per tick (`BucketLog`); a run ends
  when the controller decides ROLLING_BACK, decides to promote, emits a stall, or at 36 h.

`module11 sim verify` runs every case in ACCEPTANCE at its full size (one worker per core) and
writes module11/release/controller_sim_report.json keyed by the closureSha256 of this file's
import closure (release/closure.py). `M11_SIM_RUNS` caps the runs per case for CLI runs.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from . import controller as ctl
from . import signals as sig
from . import stats
from .router_spec import PROFILES, WATCH_TOOLS

REPORT_PATH = "module11/release/controller_sim_report.json"
SIM_ENTRY = "module11/release/rollout_sim.py"
DURATIONS_PATH = "tests/fixtures/module11/session_durations.json"
TOOL_RATES = {"query_metrics": 0.95, "search_runbooks": 0.95, "get_service_logs": 0.60}
TICK = 60.0
BATCH_HORIZON_MIN = 36 * 60
DEMO_HORIZON = 20 * 60
ROLLOUT_ID = "r-5e1f00d5e1f00d5e"
VERSIONS = {"stableVersion": 7, "candidateVersion": 8}
# The B5 / S7 traffic shape: `traffic --sessions 180 --concurrency 20 --mix payments=0.6`. It was 120 sessions; at
# measured session durations (median about 12 s, not ~30 s) 120 ended at about t0 + 1:45, before a second counted
# tick in too many runs. 180 is the smallest count in 160-200 that meets DEMO_POWER at R10's rate with the demo
# null bound held.
S7_SESSIONS = 180
# R10, the v8 effect size (as calibrated on dev): the payments search_runbooks
# rate on the candidate at most candidateMax and on stable at least stableMin, over minSessionsPerArm sessions per
# arm. candidateMax is the conservative rate demo-power-nominal is proven at: the higher of the worst measured run
# of the shipped wording (12/52 = 0.231) and the pooled rate's upper 95% bound (35/236: 0.208). It was 0.10.
R10 = {"candidateMax": 0.231, "stableMin": 0.95, "minSessionsPerArm": 30}
# The acceptance thresholds, copied into every report row. This constant is inside the closure,
# so loosening a threshold changes closureSha256 and needs a fresh report.
NULL = {"maxCpUpper": 0.01, "maxStalled": 0}
POWER = {"minPower": 0.80}
DEMO_POWER = {"minRolledBackBy430": 0.90, "minRolledBackBy500": 0.99, "maxPromoted": 0.001}
DEMO_NULL = {"maxRolledBack": 0.05}


@dataclass(frozen=True)
class Case:
    name: str
    profile: str
    kind: str                    # s7 | poisson (demo) | batch
    runs: int
    threshold: dict
    rate: float = 0.0            # sessions per minute (poisson, batch)
    base: float = 0.02           # base error and tool-failure rate
    effect: dict = field(default_factory=dict)
    fast: bool = False           # also run by `ci validate` / test_demo_* at M11_SIM_RUNS


def _cases() -> list[Case]:
    cases = [
        Case("demo-power-nominal", "demo", "s7", 10000, DEMO_POWER,
             effect={"payments_search_runbooks": R10["candidateMax"]}, fast=True),
        Case("demo-null-s7", "demo", "s7", 10000, DEMO_NULL, fast=True),
        Case("demo-null-rate20", "demo", "poisson", 5000, DEMO_NULL, rate=20),
    ]
    for profile, rates in (("prod", (30, 120)), ("model", (30, 120))):
        for base in (0.002, 0.005, 0.02, 0.05):
            for rate in rates:
                cases.append(Case(f"{profile}-null-{rate}-{base}", profile, "batch", 4000, NULL, rate=rate, base=base))
    for rate in (10, 600):
        cases.append(Case(f"prod-null-{rate}-0.02", "prod", "batch", 4000, NULL, rate=rate, base=0.02))
    for rate in (30, 120, 600):
        cases.append(Case(f"prod-power-error-{rate}", "prod", "batch", 400, POWER, rate=rate, effect={"error": 0.06}))
    for rate in (30, 120):
        cases.append(Case(f"prod-power-toolfail-{rate}", "prod", "batch", 300, POWER, rate=rate,
                          effect={"tool_failure": 0.06}))
    cases.append(Case("prod-power-quality-120", "prod", "batch", 200, POWER, rate=120, effect={"quality": -0.10}))
    return cases


CASES = _cases()
ACCEPTANCE = {c.name: c.threshold for c in CASES}


def case(name: str) -> Case:
    return next(c for c in CASES if c.name == name)


def sim_runs(default: int) -> int:
    """Runs per case: M11_SIM_RUNS caps the case's full size."""
    env = os.environ.get("M11_SIM_RUNS")
    return min(default, int(env)) if env else default


def _seed(case_name: str, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{case_name}:{index}".encode()).digest()[:8], "big")


def load_durations(root: str | Path) -> list[float]:
    doc = json.loads((Path(root) / DURATIONS_PATH).read_text(encoding="utf-8"))
    return [float(x) for x in doc["durationsSeconds"]]


def _open(profile: str, now: float) -> dict:
    stable = {"version": VERSIONS["stableVersion"], "manifestVersion": "2026-10-12T09:14:55Z/3c8e1b0",
              "behaviorDigest": "sha256:" + "0" * 64, "release": "harness-v7"}
    item = {"state": "IDLE", "revision": 0, "endpoints": {"stable": stable}}
    d = ctl.decide(item, "open", {"rolloutId": ROLLOUT_ID, "profile": profile, **VERSIONS,
                                  "behaviorDigest": "sha256:" + "1" * 64,
                                  "manifestVersion": "2026-10-13T19:00:00Z/abcdef0", "release": "harness-v8"},
                   None, None, now)
    item = d.item
    if item["state"] == "SHADOWING":
        # The model profile's canary phase only: its shadow phase routes no user traffic.
        item = ctl.decide(item, "shadow-pass", {}, PROFILES[profile], None, now).item
    return item


# ---------------------------------------------------------------- demo profile (session level)
def _demo_session(rnd: random.Random, arm: str, team: str, start: float, duration: float, effect: dict,
                  base: float) -> sig.Session:
    selected = set()
    for tool, rate in TOOL_RATES.items():
        if arm == "candidate" and team == "payments" and tool == "search_runbooks" and \
                "payments_search_runbooks" in effect:
            rate = effect["payments_search_runbooks"]
        if rnd.random() < rate:
            selected.add(tool)
    return sig.Session(arm=arm, end=start + duration, error=rnd.random() < base, tool_failure=rnd.random() < base,
                       offered=WATCH_TOOLS, selected=frozenset(selected), latency=duration * 1000.0,
                       cost=0.02 * math.exp(rnd.gauss(0, 0.3)), cache=0.8)


def _poll_window(item: dict, start: float, ready_at: float, qualifier: str, version: int) -> tuple[dict, float | None]:
    """The controller's in-invocation poll: every 5 s for up to 40 s."""
    t = start
    while t + ctl.POLL_INTERVAL <= start + ctl.POLL_WINDOW + 1e-9:
        t += ctl.POLL_INTERVAL
        status = {"status": "READY", "liveVersion": version} if t >= ready_at else \
            {"status": "UPDATING", "liveVersion": None, "targetVersion": version}
        d = ctl.decide(item, "tick", {}, PROFILES[item["profile"]], None, t, {"endpoints": {qualifier: status}})
        if d.transitions:
            return d.item, t
    return item, None


def run_demo(case: Case, index: int, durations: list[float]) -> dict:
    """One demo rollout. t0 = traffic start = 0; open 20 s before (18:50 vs 19:10)."""
    rnd = random.Random(_seed(case.name, index))
    profile = PROFILES[case.profile]
    item = _open(case.profile, -20.0)
    log = sig.SessionLog([], item["stepStarts"])
    tick = -20.0 + rnd.uniform(0.0, TICK)
    ready_range = (30.0, 75.0)
    starts: list[tuple[float, str]] = []
    if case.kind == "s7":
        pools = ["payments"] * 12 + ["platform"] * 8
        starts = sorted((rnd.uniform(0.0, 2.0), team) for team in pools)
        remaining = S7_SESSIONS
    else:
        remaining = None
    poisson_next = rnd.expovariate(case.rate / 60.0) if case.kind == "poisson" else None
    horizon = DEMO_HORIZON if case.kind == "s7" else 120 * 60.0
    issued = ready_at = None
    outcome, at = "OBSERVING", None
    while tick <= horizon:
        pct = item["canaryPct"] if item["state"] in ("WARMING", "OBSERVING", "ADVANCING") else 0
        new = []
        if case.kind == "s7":
            while starts and starts[0][0] < tick and remaining > 0:
                s_at, team = starts.pop(0)
                arm = "candidate" if rnd.random() * 100 < pct else "stable"
                duration = rnd.choice(durations)
                new.append(_demo_session(rnd, arm, team, s_at, duration, case.effect, case.base))
                remaining -= 1
                bisect.insort(starts, (s_at + duration + rnd.uniform(0.2, 1.0), team))
        else:
            while poisson_next < tick:
                team = "payments" if rnd.random() < 0.6 else "platform"
                arm = "candidate" if rnd.random() * 100 < pct else "stable"
                new.append(_demo_session(rnd, arm, team, poisson_next, rnd.choice(durations), case.effect, case.base))
                poisson_next += rnd.expovariate(case.rate / 60.0)
        log.add(new)
        if item["state"] in ("ROLLING_BACK", "ADVANCING"):
            qualifier = "candidate" if item["state"] == "ROLLING_BACK" else "stable"
            version = item["stableVersion"] if qualifier == "candidate" else item["candidateVersion"]
            item, done = _poll_window(item, tick, ready_at, qualifier, version)
        else:
            d = ctl.decide(item, "tick", {}, profile, log, tick)
            item = d.item
            log.set_steps(item["stepStarts"])
            done = None
            if item["state"] in ("ROLLING_BACK", "ADVANCING") and issued is None:
                issued = tick
                ready_at = tick + rnd.uniform(*ready_range)
                qualifier = "candidate" if item["state"] == "ROLLING_BACK" else "stable"
                version = item["stableVersion"] if qualifier == "candidate" else item["candidateVersion"]
                item, done = _poll_window(item, tick, ready_at, qualifier, version)
        if item["state"] in ("ROLLED_BACK", "PROMOTED"):
            outcome, at = item["state"], done
            break
        tick += TICK
    if outcome == "OBSERVING":
        outcome = item["state"]
    return {"outcome": outcome, "at": at, "reason": item.get("reason")}


# ---------------------------------------------------------------- batch profiles (tick aggregates)
class BucketLog:
    """A `signals.Log` over per-tick aggregates. Every bucket ends at a tick time and belongs to the
    step that was current while it filled, so windows (t_from, t_to] at tick times are exact."""

    def __init__(self, open_at: float) -> None:
        self.t_end: list[float] = []
        self.step_first: list[int] = []        # first bucket index of each step
        self.cum = {arm: {"n": [0], "err": [0], "tf": [0], "cost_n": [0], "cost_s": [0.0], "cost_q": [0.0],
                          **{t: [0] for t in WATCH_TOOLS}} for arm in sig.ARMS}
        self.lat = {arm: [] for arm in sig.ARMS}
        self.q = {arm: [0, 0.0, 0.0] for arm in sig.ARMS}          # arrived n, sum, sumsq
        self.q_bins = {arm: np.zeros((40, 3)) for arm in sig.ARMS}  # ring buffer by arrival tick
        self.open_at = open_at

    def add(self, t_end: float, step: int, arms: dict[str, dict]) -> None:
        index = len(self.t_end)
        while len(self.step_first) <= step:
            self.step_first.append(index)
        self.t_end.append(t_end)
        for arm in sig.ARMS:
            a, cum = arms[arm], self.cum[arm]
            cum["n"].append(cum["n"][-1] + a["n"])
            cum["err"].append(cum["err"][-1] + a["err"])
            cum["tf"].append(cum["tf"][-1] + a["tf"])
            cum["cost_n"].append(cum["cost_n"][-1] + a["n"])
            cum["cost_s"].append(cum["cost_s"][-1] + a["cost_s"])
            cum["cost_q"].append(cum["cost_q"][-1] + a["cost_q"])
            for t in WATCH_TOOLS:
                cum[t].append(cum[t][-1] + a["sel"][t])
            self.lat[arm].append(a["lat"])
            ring = self.q_bins[arm]
            if a.get("q") is not None:
                delays, values = a["q"]
                for col, w in ((0, None), (1, values), (2, values * values)):
                    ring[:, col] += np.bincount((index + delays) % 40, weights=w, minlength=40)
            row = ring[index % 40]
            self.q[arm][0] += int(row[0])
            self.q[arm][1] += row[1]
            self.q[arm][2] += row[2]
            ring[index % 40] = 0.0

    def _range(self, step: int, t_from: float, t_to: float) -> tuple[int, int]:
        lo = bisect.bisect_right(self.t_end, t_from)
        hi = bisect.bisect_right(self.t_end, t_to)
        if step < len(self.step_first):
            lo = max(lo, self.step_first[step])
            if step + 1 < len(self.step_first):
                hi = min(hi, self.step_first[step + 1])
        else:
            lo = hi
        return lo, max(lo, hi)

    def count(self, arm, step, t_from, t_to):
        lo, hi = self._range(step, t_from, t_to)
        n = self.cum[arm]["n"]
        return n[hi] - n[lo]

    def agg(self, arm, step, t_from, t_to):
        lo, hi = self._range(step, t_from, t_to)
        cum = self.cum[arm]
        n = cum["n"][hi] - cum["n"][lo]
        lat = self.lat[arm][lo:hi]
        return sig.ArmAgg(n=n, errors=cum["err"][hi] - cum["err"][lo], tool_failures=cum["tf"][hi] - cum["tf"][lo],
                          offered={t: n for t in WATCH_TOOLS},
                          selected={t: cum[t][hi] - cum[t][lo] for t in WATCH_TOOLS},
                          latencies=np.concatenate(lat) if lat else np.zeros(0),
                          cost_n=n, cost_sum=cum["cost_s"][hi] - cum["cost_s"][lo],
                          cost_sumsq=cum["cost_q"][hi] - cum["cost_q"][lo], cache_n=n, cache_sum=0.8 * n)

    def kth_latest_end(self, arm, step, k, t_to):  # pragma: no cover - sliding mode only
        raise NotImplementedError

    def first_end(self, arm, step):
        if step >= len(self.step_first):
            return None
        lo = self.step_first[step]
        hi = self.step_first[step + 1] if step + 1 < len(self.step_first) else len(self.t_end)
        n = self.cum[arm]["n"]
        j = bisect.bisect_right(n, n[lo], lo)          # first cumulative count above the step's start
        return self.t_end[j - 1] if lo < j <= hi else None

    def candidate_count(self, t_from, t_to):
        lo = bisect.bisect_right(self.t_end, t_from)
        hi = bisect.bisect_right(self.t_end, t_to)
        n = self.cum["candidate"]["n"]
        return n[hi] - n[lo]

    def strata(self, t_to):
        out = {"error_rate": [], "tool_failure": []}
        hi_all = bisect.bisect_right(self.t_end, t_to)
        for step, lo in enumerate(self.step_first):
            hi = min(hi_all, self.step_first[step + 1] if step + 1 < len(self.step_first) else hi_all)
            c, s = self.cum["candidate"], self.cum["stable"]
            out["error_rate"].append((c["err"][hi] - c["err"][lo], c["n"][hi] - c["n"][lo],
                                      s["err"][hi] - s["err"][lo], s["n"][hi] - s["n"][lo]))
            out["tool_failure"].append((c["tf"][hi] - c["tf"][lo], c["n"][hi] - c["n"][lo],
                                        s["tf"][hi] - s["tf"][lo], s["n"][hi] - s["n"][lo]))
        return out

    def quality(self, arm, t_to):
        n, total, sq = self.q[arm]
        if n == 0:
            return {}
        m = total / n
        var = max(0.0, (sq - n * m * m) / (n - 1)) if n > 1 else 0.0
        return {"judge": sig.QualityAgg(n, m, var)}


def _arm_bucket(g: np.random.Generator, n: int, err: float, tf: float, quality_shift: float,
                eval_share: float) -> dict:
    sel = {t: int(g.binomial(n, r)) if n else 0 for t, r in TOOL_RATES.items()}
    if n:
        lat = g.lognormal(math.log(9000.0), 0.35, n)
        costs = 0.02 * np.exp(g.normal(0.0, 0.3, n))
        k = int(g.binomial(n, eval_share))
        if k:
            values = np.clip(g.normal(0.80 + quality_shift, 0.25, k), 0.0, 1.0)
            q = (g.integers(10, 31, k), values)
        else:
            q = None
        return {"n": n, "err": int(g.binomial(n, err)), "tf": int(g.binomial(n, tf)), "sel": sel, "lat": lat,
                "cost_s": float(costs.sum()), "cost_q": float((costs * costs).sum()), "q": q}
    return {"n": 0, "err": 0, "tf": 0, "sel": sel, "lat": np.zeros(0), "cost_s": 0.0, "cost_q": 0.0, "q": None}


def run_batch(case: Case, index: int) -> dict:
    g = np.random.Generator(np.random.PCG64(_seed(case.name, index)))
    profile = PROFILES[case.profile]
    item = _open(case.profile, 0.0)
    log = BucketLog(0.0)
    phase = float(g.uniform(0.0, TICK))
    prev, tick = 0.0, phase
    c_err = case.effect.get("error", case.base)
    c_tf = case.effect.get("tool_failure", case.base)
    c_q = case.effect.get("quality", 0.0)
    while tick <= BATCH_HORIZON_MIN * 60:
        pct = item["canaryPct"] if item["state"] in ("WARMING", "OBSERVING", "ADVANCING") else 0
        total = int(g.poisson(case.rate * (tick - prev) / 60.0))
        n_c = int(g.binomial(total, pct / 100.0)) if pct else 0
        log.add(tick, int(item["stepIndex"]), {
            "candidate": _arm_bucket(g, n_c, c_err, c_tf, c_q, 1.0),
            "stable": _arm_bucket(g, total - n_c, case.base, case.base, 0.0, 0.10)})
        d = ctl.decide(item, "tick", {}, profile, log, tick)
        item = d.item
        if any(a.kind == "emit" and a.args.get("name") == "m11.rollout.stalled" for a in d.actions):
            return {"outcome": "STALLED", "minutes": tick / 60, "looks": item.get("looks", 0),
                    "reason": item.get("stalled")}
        if item["state"] == "ROLLING_BACK":
            return {"outcome": "ROLLED_BACK", "minutes": tick / 60, "looks": item.get("looks", 0),
                    "reason": item.get("reason")}
        if item["state"] == "ADVANCING":
            return {"outcome": "PROMOTED", "minutes": tick / 60, "looks": item.get("looks", 0), "reason": None}
        prev, tick = tick, tick + TICK
    return {"outcome": "TIMEOUT", "minutes": tick / 60, "looks": item.get("looks", 0), "reason": None}


# ---------------------------------------------------------------- running and reporting
def _run_chunk(job: tuple[str, int, int, str]) -> list[dict]:
    name, lo, hi, root = job
    c = case(name)
    if c.kind == "batch":
        return [run_batch(c, i) for i in range(lo, hi)]
    durations = load_durations(root)
    return [run_demo(c, i, durations) for i in range(lo, hi)]


def summarize(c: Case, outcomes: list[dict]) -> dict:
    runs = len(outcomes)
    count = lambda o: sum(1 for x in outcomes if x["outcome"] == o)  # noqa: E731
    rolled, promoted, stalled = count("ROLLED_BACK"), count("PROMOTED"), count("STALLED")
    row: dict[str, Any] = {"case": c.name, "profile": c.profile, "rate": c.rate or None, "base": c.base,
                           "effect": c.effect or None, "runs": runs, "rolledBack": rolled, "promoted": promoted,
                           "stalled": stalled, "threshold": dict(c.threshold)}
    if c.kind == "batch":
        row["cpUpper"] = round(stats.clopper_pearson_upper(rolled, runs), 6)
        row["maxLooks"] = max((x["looks"] for x in outcomes), default=0)
        row["power"] = round(rolled / runs, 6)
        times = sorted(x["minutes"] for x in outcomes if x["outcome"] == ("ROLLED_BACK" if c.effect else "PROMOTED"))
        row["medianMinutes"] = round(times[len(times) // 2], 1) if times else None
        reasons: dict[str, int] = {}
        for x in outcomes:
            if x["outcome"] == "ROLLED_BACK":
                reasons[str(x["reason"])] = reasons.get(str(x["reason"]), 0) + 1
        row["reasons"] = dict(sorted(reasons.items()))
    else:
        times = sorted(x["at"] for x in outcomes if x["outcome"] == "ROLLED_BACK" and x["at"] is not None)
        row["rolledBackBy430"] = round(sum(1 for t in times if t <= 270) / runs, 6)
        row["rolledBackBy500"] = round(sum(1 for t in times if t <= 300) / runs, 6)
        row["pRolledBack"] = round(rolled / runs, 6)
        row["pPromoted"] = round(promoted / runs, 6)
        row["medianRolledBackSeconds"] = round(times[len(times) // 2], 1) if times else None
    row["passed"] = meets(row)
    return row


def meets(row: dict) -> bool:
    t, runs = row["threshold"], row["runs"]
    checks = []
    if "maxCpUpper" in t:
        checks.append(row["cpUpper"] <= t["maxCpUpper"])
    if "maxStalled" in t:
        checks.append(row["stalled"] <= t["maxStalled"])
    if "minPower" in t:
        checks.append(row["rolledBack"] / runs >= t["minPower"])
    if "minRolledBackBy430" in t:
        checks.append(row["rolledBackBy430"] >= t["minRolledBackBy430"])
    if "minRolledBackBy500" in t:
        checks.append(row["rolledBackBy500"] >= t["minRolledBackBy500"])
    if "maxPromoted" in t:
        checks.append(row["promoted"] / runs <= t["maxPromoted"])
    if "maxRolledBack" in t:
        checks.append(row["rolledBack"] / runs <= t["maxRolledBack"])
    return all(checks) and bool(checks)


def run(names: Iterable[str], root: str | Path, runs: dict[str, int] | None = None, workers: int | None = None,
        chunk: int = 50) -> list[dict]:
    """Run cases (full size unless `runs` overrides) and return their report rows."""
    names = list(names)
    sizes = {n: (runs or {}).get(n, case(n).runs) for n in names}
    jobs = [(n, lo, min(lo + chunk, sizes[n]), str(root)) for n in names for lo in range(0, sizes[n], chunk)]
    workers = workers if workers is not None else (os.cpu_count() or 1)
    if workers <= 1:
        results = [_run_chunk(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_run_chunk, jobs))
    by_case: dict[str, list[dict]] = {n: [] for n in names}
    for job, res in zip(jobs, results):
        by_case[job[0]].extend(res)
    return [summarize(case(n), by_case[n]) for n in names]


def closure_sha256(root: str | Path) -> str:
    """The multi-file artifact hash over the simulator's whole import closure."""
    from .closure import closure
    from .hashing import file_hash, files_hash
    base = Path(root)
    return files_hash({p: file_hash(p, (base / p).read_bytes()) for p in closure(SIM_ENTRY, base)})


def durations_sha256(root: str | Path) -> str:
    from .hashing import file_hash
    return file_hash(DURATIONS_PATH, (Path(root) / DURATIONS_PATH).read_bytes())


def report(root: str | Path, rows: list[dict], cut: str | None = None) -> dict:
    return {"closureSha256": closure_sha256(root), "inputs": {"sessionDurationsSha256": durations_sha256(root)},
            "generatedBy": "python -m module11 sim verify", "cut": cut, "rows": rows,
            "passed": all(r["passed"] for r in rows)}


def write_report(root: str | Path, doc: dict) -> Path:
    path = Path(root) / REPORT_PATH
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_report(root: str | Path) -> dict | None:
    path = Path(root) / REPORT_PATH
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def report_problems(root: str | Path, doc: dict | None = None) -> list[str]:
    """Why the committed report is not current (test_controller_sim_report_current)."""
    doc = doc if doc is not None else read_report(root)
    if doc is None:
        return [f"{REPORT_PATH} is missing: run `module11 sim verify`"]
    problems = []
    if doc.get("closureSha256") != closure_sha256(root):
        problems.append("closureSha256 differs from the simulator closure: run `module11 sim verify`")
    if (doc.get("inputs") or {}).get("sessionDurationsSha256") != durations_sha256(root):
        problems.append("session_durations.json changed since the report: run `module11 sim verify`")
    if doc.get("passed") is not True:
        problems.append("report has passed=false")
    names = {r["case"] for r in doc.get("rows", [])}
    for missing in sorted(set(ACCEPTANCE) - names):
        problems.append(f"report has no row for {missing}")
    for row in doc.get("rows", []):
        if row.get("threshold") != ACCEPTANCE.get(row["case"]):
            problems.append(f"{row['case']}: stored threshold differs from ACCEPTANCE")
        elif not meets(row):
            problems.append(f"{row['case']}: row does not meet its threshold")
    return problems
