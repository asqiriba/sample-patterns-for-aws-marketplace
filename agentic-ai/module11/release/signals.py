# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Canary signals for the rollback controller ("Signals" and "Breach tests"). Pure.

A signal is `breach` only if its effect threshold is exceeded AND a one-sided test at the
profile's alpha rejects "candidate no worse than stable"; `insufficient` when the sample rule
is not met; otherwise `ok`. Tests run only when the effect threshold is exceeded, so a quiet
tick costs a few comparisons.

The controller reads sessions through a `Log` (outcome items for the Lambda, `SessionLog`;
per-tick aggregates for the simulator). Times are epoch seconds; a window is (t_from, t_to].
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, Sequence

import numpy as np

from . import stats
from .router_spec import QUALITY_MIN_PER_ARM, WATCH_TOOLS

BREACH, OK, INSUFFICIENT = "breach", "ok", "insufficient"
SIGNAL_ORDER = ("error_rate", "tool_failure", "tool_selection", "latency", "cost", "quality")
QUALITY_MARGIN = 0.05
ARMS = ("candidate", "stable")


@dataclass
class ArmAgg:
    """Sufficient statistics of one arm's sessions in a window."""
    n: int = 0
    errors: int = 0
    tool_failures: int = 0
    offered: dict[str, int] = field(default_factory=dict)
    selected: dict[str, int] = field(default_factory=dict)
    latencies: Sequence[float] | np.ndarray = ()
    cost_n: int = 0
    cost_sum: float = 0.0
    cost_sumsq: float = 0.0
    cache_n: int = 0
    cache_sum: float = 0.0

    def cost_mean_var(self) -> tuple[float, float]:
        m = self.cost_sum / self.cost_n
        var = max(0.0, (self.cost_sumsq - self.cost_n * m * m) / (self.cost_n - 1)) if self.cost_n > 1 else 0.0
        return m, var

    def cache_ratio(self) -> float | None:
        return self.cache_sum / self.cache_n if self.cache_n else None


@dataclass(frozen=True)
class QualityAgg:
    n: int
    mean: float
    var: float


@dataclass(frozen=True)
class SignalResult:
    name: str
    status: str
    detail: str = ""


class Log(Protocol):
    def count(self, arm: str, step: int, t_from: float, t_to: float) -> int: ...
    def agg(self, arm: str, step: int, t_from: float, t_to: float) -> ArmAgg: ...
    def kth_latest_end(self, arm: str, step: int, k: int, t_to: float) -> float | None: ...
    def first_end(self, arm: str, step: int) -> float | None: ...
    def candidate_count(self, t_from: float, t_to: float) -> int: ...
    def strata(self, t_to: float) -> dict[str, list[tuple[int, int, int, int]]]: ...
    def quality(self, arm: str, t_to: float) -> dict[str, QualityAgg]: ...


# ---------------------------------------------------------------- signal tests
def rate_window(name: str, c: ArmAgg, s: ArmAgg, alpha: float, min_events: int) -> SignalResult:
    """Sliding-window error rate or tool-failure rate: one-sided Fisher (demo profile)."""
    a, c_events = (c.errors, s.errors) if name == "error_rate" else (c.tool_failures, s.tool_failures)
    if c.n == 0 or s.n == 0:
        return SignalResult(name, INSUFFICIENT, "empty arm")
    rc, rs = a / c.n, c_events / s.n
    effect = (rc - rs > 0.005) if name == "error_rate" else (rc > 0.02 and rc > rs + 0.01)
    if not effect or a < min_events:
        return SignalResult(name, OK, f"{a}/{c.n} vs {c_events}/{s.n}")
    p = stats.fisher_greater(a, c.n, c_events, s.n)
    return SignalResult(name, BREACH if p < alpha else OK, f"{a}/{c.n} vs {c_events}/{s.n} p={p:.3g}")


def tool_selection(c: ArmAgg, s: ArmAgg, alpha: float, min_n: int,
                   tools: Sequence[str] = WATCH_TOOLS) -> SignalResult:
    """Two-sided Fisher per watched tool at alpha / len(tools) (Bonferroni)."""
    details, insufficient = [], False
    for tool in tools:
        nc, ns = c.offered.get(tool, 0), s.offered.get(tool, 0)
        if nc == 0 and ns == 0:
            continue                      # not offered on this stage: not applicable
        if nc < min_n or ns < min_n:
            insufficient = True
            continue
        kc, ks = c.selected.get(tool, 0), s.selected.get(tool, 0)
        rc, rs = kc / nc, ks / ns
        if abs(rc - rs) / max(rs, 0.05) <= 0.30:
            continue
        p = stats.fisher_two_sided(kc, nc, ks, ns)
        if p < alpha / len(tools):
            return SignalResult("tool_selection", BREACH, f"{tool} {kc}/{nc} vs {ks}/{ns} p={p:.3g}")
        details.append(f"{tool} p={p:.3g}")
    return SignalResult("tool_selection", INSUFFICIENT if insufficient else OK, ", ".join(details))


def _warm(c: ArmAgg, s: ArmAgg) -> bool:
    rc, rs = c.cache_ratio(), s.cache_ratio()
    return rc is not None and rs is not None and rc >= rs - 0.2


def latency(c: ArmAgg, s: ArmAgg, alpha: float, min_n: int) -> SignalResult:
    """p95: candidate > 1.25 x stable, and a one-sided exact exceedance test that more candidate sessions exceed
    stable's window p95 (its m-th order statistic, m = ceil(0.95 n)) than chance allows when both arms share one
    latency distribution (`stats.exceedance_greater`). Distribution-free, so skewed latencies and small windows do
    not inflate false breaches; it replaced a binomial test against a fixed 5%, which ignored that a small
    sample's p95 is close to its maximum. Needs the sample rule and warm-up (cache-read ratio)."""
    if len(c.latencies) < min_n or len(s.latencies) < min_n or not _warm(c, s):
        return SignalResult("latency", INSUFFICIENT)
    sc, ss = np.sort(np.asarray(c.latencies)), np.sort(np.asarray(s.latencies))
    p95c = sc[max(1, math.ceil(0.95 * len(sc))) - 1]
    m = max(1, math.ceil(0.95 * len(ss)))
    p95s = ss[m - 1]
    if not p95c > 1.25 * p95s:
        return SignalResult("latency", OK)
    k = int(len(sc) - np.searchsorted(sc, p95s, side="right"))
    p = stats.exceedance_greater(k, len(sc), len(ss), m)
    return SignalResult("latency", BREACH if p < alpha else OK, f"p95 {p95c:.0f} vs {p95s:.0f}, {k}/{len(sc)} above p={p:.3g}")


def cost(c: ArmAgg, s: ArmAgg, alpha: float, min_n: int) -> SignalResult:
    """Cost per session: candidate > 1.20 x stable, one-sided Welch. Insufficient without prices."""
    if c.cost_n < min_n or s.cost_n < min_n or not _warm(c, s):
        return SignalResult("cost", INSUFFICIENT)
    mc, vc = c.cost_mean_var()
    ms, vs = s.cost_mean_var()
    if not mc > 1.20 * ms:
        return SignalResult("cost", OK)
    p = stats.welch_greater(mc, vc, c.cost_n, ms, vs, s.cost_n)
    return SignalResult("cost", BREACH if p < alpha else OK, f"{mc:.4f} vs {ms:.4f} p={p:.3g}")


def quality(qc: Mapping[str, QualityAgg], qs: Mapping[str, QualityAgg], alpha: float,
            min_n: int = QUALITY_MIN_PER_ARM) -> SignalResult:
    """Online-eval quality over the whole rollout: n >= 200 per arm per gated evaluator; breach when
    any evaluator's candidate mean < stable - 0.05 with a one-sided Welch p < alpha."""
    evaluators = sorted(set(qc) | set(qs))
    if not evaluators:
        return SignalResult("quality", INSUFFICIENT, "no scores")
    for ev in evaluators:
        if ev not in qc or ev not in qs or qc[ev].n < min_n or qs[ev].n < min_n:
            return SignalResult("quality", INSUFFICIENT, f"{ev} below {min_n} per arm")
    for ev in evaluators:
        c, s = qc[ev], qs[ev]
        if c.mean < s.mean - QUALITY_MARGIN:
            p = stats.welch_less(c.mean, c.var, c.n, s.mean, s.var, s.n)
            if p < alpha:
                return SignalResult("quality", BREACH, f"{ev} {c.mean:.3f} vs {s.mean:.3f} p={p:.3g}")
    return SignalResult("quality", OK)


def pooled_rate(name: str, strata: Sequence[tuple[int, int, int, int]], alpha_look: float,
                min_events: int) -> SignalResult:
    """Batch profiles: the exact stratified test over the rollout's steps with RD_MH effects."""
    a = sum(s[0] for s in strata)
    n1 = sum(s[1] for s in strata)
    if n1 == 0:
        return SignalResult(name, INSUFFICIENT)
    rd = stats.rd_mh(strata)
    if name == "error_rate":
        effect = rd > 0.005
    else:
        effect = rd > 0.01 and a / n1 > 0.02
    if not effect or a < min_events:
        return SignalResult(name, OK, f"RD_MH={rd:.4f}")
    p = stats.stratified_exact_greater(strata)
    return SignalResult(name, BREACH if p < alpha_look else OK, f"RD_MH={rd:.4f} p={p:.3g}")


# ---------------------------------------------------------------- the Lambda's log: outcome items
@dataclass
class Session:
    arm: str
    end: float
    error: bool = False
    tool_failure: bool = False
    offered: tuple[str, ...] = ()
    selected: frozenset[str] = frozenset()
    latency: float | None = None
    cost: float | None = None
    cache: float | None = None


@dataclass(frozen=True)
class Score:
    arm: str
    evaluator: str
    value: float
    at: float


class SessionLog:
    """A `Log` over individual sessions; each is assigned to the step whose start precedes its end."""

    def __init__(self, sessions: Iterable[Session], step_starts: Sequence[float],
                 scores: Iterable[Score] = ()) -> None:
        self.step_starts = list(step_starts)
        self._by: dict[tuple[str, int], list[Session]] = {}
        self._ends: dict[tuple[str, int], list[float]] = {}
        self._cand_ends: list[float] = []
        self.add(sessions)
        self.scores = sorted(scores, key=lambda s: s.at)

    def add(self, sessions: Iterable[Session]) -> None:
        for s in sessions:
            step = max(0, bisect.bisect_right(self.step_starts, s.end) - 1)
            key = (s.arm, step)
            ends = self._ends.setdefault(key, [])
            rows = self._by.setdefault(key, [])
            i = bisect.bisect_right(ends, s.end)
            ends.insert(i, s.end)
            rows.insert(i, s)
            if s.arm == "candidate":
                bisect.insort(self._cand_ends, s.end)

    def set_steps(self, step_starts: Sequence[float]) -> None:
        if list(step_starts) != self.step_starts:
            sessions = [s for rows in self._by.values() for s in rows]
            self.__init__(sessions, step_starts, self.scores)

    def _slice(self, arm: str, step: int, t_from: float, t_to: float) -> tuple[int, int]:
        ends = self._ends.get((arm, step), [])
        return bisect.bisect_right(ends, t_from), bisect.bisect_right(ends, t_to)

    def count(self, arm, step, t_from, t_to):
        lo, hi = self._slice(arm, step, t_from, t_to)
        return hi - lo

    def agg(self, arm, step, t_from, t_to):
        lo, hi = self._slice(arm, step, t_from, t_to)
        rows = self._by.get((arm, step), [])[lo:hi]
        out = ArmAgg(n=len(rows))
        lat = []
        for s in rows:
            out.errors += s.error
            out.tool_failures += s.tool_failure
            for t in s.offered:
                out.offered[t] = out.offered.get(t, 0) + 1
                if t in s.selected:
                    out.selected[t] = out.selected.get(t, 0) + 1
            if s.latency is not None:
                lat.append(s.latency)
            if s.cost is not None:
                out.cost_n += 1
                out.cost_sum += s.cost
                out.cost_sumsq += s.cost * s.cost
            if s.cache is not None:
                out.cache_n += 1
                out.cache_sum += s.cache
        out.latencies = lat
        return out

    def kth_latest_end(self, arm, step, k, t_to):
        ends = self._ends.get((arm, step), [])
        hi = bisect.bisect_right(ends, t_to)
        return ends[hi - k] if hi >= k else None

    def first_end(self, arm, step):
        ends = self._ends.get((arm, step), [])
        return ends[0] if ends else None

    def candidate_count(self, t_from, t_to):
        return bisect.bisect_right(self._cand_ends, t_to) - bisect.bisect_right(self._cand_ends, t_from)

    def strata(self, t_to):
        out: dict[str, list[tuple[int, int, int, int]]] = {"error_rate": [], "tool_failure": []}
        for step in range(len(self.step_starts)):
            c = self.agg("candidate", step, float("-inf"), t_to)
            s = self.agg("stable", step, float("-inf"), t_to)
            out["error_rate"].append((c.errors, c.n, s.errors, s.n))
            out["tool_failure"].append((c.tool_failures, c.n, s.tool_failures, s.n))
        return out

    def quality(self, arm, t_to):
        acc: dict[str, list[float]] = {}
        for sc in self.scores:
            if sc.at > t_to:
                break
            if sc.arm == arm:
                acc.setdefault(sc.evaluator, []).append(sc.value)
        return {ev: QualityAgg(len(v), stats.mean(v), stats.sd(v) ** 2 if len(v) > 1 else 0.0) for ev, v in acc.items()}


def session_cost(item: Mapping, price: Mapping | None) -> float | None:
    """USD for one session from prices.yaml's per-1k-token prices; None while any price is null."""
    keys = ("inputPer1kTokens", "cachedInputPer1kTokens", "outputPer1kTokens")
    if not price or any(price.get(k) is None for k in keys):
        return None
    tokens_in = float(item.get("inputTokens") or 0)
    cached = float(item.get("cachedInputTokens") or 0)
    tokens_out = float(item.get("outputTokens") or 0)
    return ((tokens_in - cached) * price["inputPer1kTokens"] + cached * price["cachedInputPer1kTokens"]
            + tokens_out * price["outputPer1kTokens"]) / 1000.0


def session_from_outcome(item: Mapping, price: Mapping | None = None) -> Session:
    """An outcome item as a Session. `price` is the session model's prices.yaml
    entry; a null price leaves the cost signal insufficient."""
    offered = tuple(item.get("toolsOffered") or ())
    tokens_in = float(item.get("inputTokens") or 0)
    cached = float(item.get("cachedInputTokens") or 0)
    cost_per_session = session_cost(item, price)
    return Session(
        arm=item["arm"], end=float(str(item["sk"]).split("#")[0]) / 1000.0 if "sk" in item else float(item["end"]),
        error=item.get("status") not in ("ok", "completed", "succeeded"),
        tool_failure=bool(item.get("toolFailures")), offered=offered,
        selected=frozenset(item.get("toolsExecuted") or ()), latency=item.get("latencyMs"),
        cost=cost_per_session, cache=(cached / tokens_in) if tokens_in else None)
