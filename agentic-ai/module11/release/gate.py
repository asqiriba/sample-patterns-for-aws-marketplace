# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Gate engine. Pure: no AWS, no clock.

`evaluate()` turns one run's evaluation results and session metrics into a GateReport with a
verdict PASS | FAIL | INCONCLUSIVE (exit codes 0, 2, 3). Rules:
1. hard gates: every (scenario, repeat) in scope has value >= passAt; a missing value is
   INCONCLUSIVE, never PASS;
2. statistical gates: mean >= minMean and mean >= baseline - max(maxRegression, 2 noise_r),
   compared only with the same (evaluator, category, scope) record; noise_r scales the
   calibration noise by sqrt(r_cal / r);
3. budgets: absolute and relative to the scope's baseline budgets;
4. coverage: fewer than 90% of the expected results present is INCONCLUSIVE;
5. FAIL beats INCONCLUSIVE beats PASS. A suite that differs from the baseline's makes the
   whole report INCONCLUSIVE.
An MR's gate definition always comes from `main` (release/preview_inputs.py); the MR's
own evaluation files only feed `informational_table()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import stats
from .hashing import json_hash

PASS, FAIL, INCONCLUSIVE, SKIP = "PASS", "FAIL", "INCONCLUSIVE", "SKIP"
INFO = "INFO"                # reported, never part of the verdict (the user-experience latency budget)
UX_BUDGET = "p95LatencyMs (user-experience budget)"
CONTROL_LINE = "p95LatencyMs vs control"
EXIT_CODES = {PASS: 0, FAIL: 2, INCONCLUSIVE: 3}
COVERAGE_MIN = 0.90
INFORMATIONAL_MAX = 12   # changed scenarios run for the MR note

Key = tuple[str, str, str]   # (evaluator, category, scope)


class ThresholdBelowNoise(ValueError):
    """maxRegressionVsBaseline is below the full-suite noise floor at the staging repeats."""


@dataclass(frozen=True)
class EvalResult:
    scenarioId: str
    category: str
    tags: tuple[str, ...]
    repeat: int
    sessionId: str
    evaluatorId: str
    value: float | None
    label: str | None = None
    errorCode: str | None = None


@dataclass(frozen=True)
class SessionMetrics:
    sessionId: str
    latencyMs: float
    inputTokens: int
    outputTokens: int
    toolCalls: int
    status: str = "ok"


@dataclass
class Baseline:
    manifestVersion: str
    evaluatorSetVersion: str
    suiteSha256: str
    stats: dict[Key, tuple[float, float, int]]
    budgets: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "Baseline":
        return cls(doc["manifestVersion"], str(doc["evaluatorSetVersion"]), doc["suiteSha256"],
                   {_key(k): tuple(v) for k, v in (doc.get("stats") or {}).items()}, dict(doc.get("budgets") or {}))


@dataclass
class NoiseFloor:
    r_cal: int
    sd: dict[Key, float]
    budget_sd: dict[str, dict[str, float]] = field(default_factory=dict)    # scope -> budget metric -> run-to-run SD

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "NoiseFloor":
        return cls(int(doc["r_cal"]), {_key(k): float(v) for k, v in (doc.get("sd") or {}).items()},
                   {s: {k: float(v) for k, v in m.items()} for s, m in (doc.get("budgetSd") or {}).items()})


def _key(text: str | Sequence[str]) -> Key:
    """Records store keys as 'evaluator|category|scope' strings."""
    parts = text.split("|") if isinstance(text, str) else list(text)
    if len(parts) != 3:
        raise ValueError(f"bad baseline key {text!r}")
    return parts[0], parts[1], parts[2]


def key_str(key: Key) -> str:
    return "|".join(key)


@dataclass
class GateLine:
    kind: str            # hard | statistical | budget | coverage | suite
    name: str
    scope: str
    status: str
    observed: str = ""
    threshold: str = ""
    detail: str = ""


@dataclass
class GateReport:
    verdict: str
    lines: list[GateLine]
    comparable: bool
    scope: str = "full"
    reasons: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def markdownTable(self) -> str:
        rows = ["| Gate | Scope | Result | Observed | Threshold |", "|---|---|---|---|---|"]
        for line in self.lines:
            observed = line.observed + (f" ({line.detail})" if line.detail else "")
            rows.append(f"| {line.name} | {line.scope} | {line.status} | {observed} | {line.threshold} |")
        head = f"**Gate: {self.verdict}** (scope `{self.scope}`)"
        if self.reasons:
            head += ": " + "; ".join(self.reasons)
        return head + "\n\n" + "\n".join(rows) + "\n"


# ---------------------------------------------------------------- scopes
def in_scope(scenario: Mapping[str, Any], scope: str) -> bool:
    """A run scope: full | tag:<t>."""
    if scope == "full":
        return True
    if scope.startswith("tag:"):
        return scope[4:] in (scenario.get("tags") or [])
    raise ValueError(f"unknown run scope {scope!r}")


def matches(scenario: Mapping[str, Any], spec: str | Sequence[str]) -> bool:
    """A gate scope entry: all | <category> | tag:<t>, or a list of them."""
    items = [spec] if isinstance(spec, str) else list(spec)
    for item in items:
        if item == "all" or item == scenario.get("category"):
            return True
        if item.startswith("tag:") and item[4:] in (scenario.get("tags") or []):
            return True
    return False


def repeats_for(profile: Mapping[str, Any], category: str) -> int:
    reps = profile.get("repeats") or {}
    return int(reps.get(category, reps.get("default", 1)))


def suite_sha256(scenarios: Iterable[Mapping[str, Any]]) -> str:
    """Identity of a scenario suite: the canonical hash of the scenarios sorted by ID."""
    return json_hash(sorted((dict(s) for s in scenarios), key=lambda s: s["scenario_id"]))


def gate_evaluators(thresholds: Mapping[str, Any]) -> list[str]:
    gates = thresholds.get("gates") or {}
    return sorted({g["evaluator"] for g in (gates.get("hard") or []) + (gates.get("statistical") or [])})


# ---------------------------------------------------------------- thresholds vs noise
def check_thresholds(thresholds: Mapping[str, Any], noise: NoiseFloor | None) -> None:
    """Raise ThresholdBelowNoise when maxRegression < noise_r[full] at the staging profile's r."""
    if noise is None:
        return
    staging = (thresholds.get("profiles") or {}).get("staging") or {}
    for gate in (thresholds.get("gates") or {}).get("statistical") or []:
        cats = [gate["scope"]] if isinstance(gate["scope"], str) else gate["scope"]
        for cat in cats:
            sd = noise.sd.get((gate["evaluator"], cat, "full"))
            if sd is None:
                continue
            noise_r = stats.scaled_noise(sd, noise.r_cal, repeats_for(staging, cat))
            if gate["maxRegressionVsBaseline"] < noise_r:
                raise ThresholdBelowNoise(
                    f"{gate['evaluator']} {cat}: maxRegressionVsBaseline {gate['maxRegressionVsBaseline']} is below "
                    f"the full-suite noise floor {noise_r:.4f} at r={repeats_for(staging, cat)}")


FLOOR_FAIL_RATE = 0.01    # calibrate before enforce: an unchanged release fails an absolute floor in < ~1% of runs
FLOOR_Z = 2.3263          # the one-sided normal quantile for FLOOR_FAIL_RATE


def derived_floor(mean: float, noise_sd: float, r_cal: int, r: int) -> float:
    """The highest minMean an unchanged release meets in 1 - FLOOR_FAIL_RATE of runs: mean - FLOOR_Z x noise_r."""
    return mean - FLOOR_Z * stats.scaled_noise(noise_sd, r_cal, r)


def floor_findings(thresholds: Mapping[str, Any], baseline: Baseline | None,
                   noise: NoiseFloor | None) -> list[dict]:
    """Statistical gates whose absolute floor (minMean) is above `derived_floor` in a profile's run scope (preview:
    tag:fast at its repeats, staging: full at its repeats), so the unchanged baseline release would fail it in
    FLOOR_FAIL_RATE or more of runs."""
    if baseline is None or noise is None:
        return []
    out = []
    for prof in ("staging", "preview"):
        profile = (thresholds.get("profiles") or {}).get(prof) or {}
        scope = profile.get("scope")
        for gate in (thresholds.get("gates") or {}).get("statistical") or []:
            for cat in [gate["scope"]] if isinstance(gate["scope"], str) else gate["scope"]:
                key = (gate["evaluator"], cat, scope)
                if key not in baseline.stats or key not in noise.sd:
                    continue
                floor = derived_floor(baseline.stats[key][0], noise.sd[key], noise.r_cal, repeats_for(profile, cat))
                if float(gate["minMean"]) > floor + 1e-12:
                    out.append({"evaluator": gate["evaluator"], "category": cat, "scope": scope,
                                "minMean": float(gate["minMean"]), "derived": round(floor, 4)})
    return out


# ---------------------------------------------------------------- evaluation
def _fmt(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") if abs(x) < 1000 else f"{x:.0f}"


def _latest(results: Iterable[EvalResult]) -> dict[tuple[str, int, str], EvalResult]:
    """One result per (scenario, repeat, evaluator); a retried evaluation's value wins over a null."""
    out: dict[tuple[str, int, str], EvalResult] = {}
    for r in results:
        k = (r.scenarioId, r.repeat, r.evaluatorId)
        if k not in out or r.value is not None:
            out[k] = r
    return out


def evaluate(thresholds: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]],
             results: Iterable[EvalResult], metrics: Iterable[SessionMetrics],
             baseline: Baseline | None, noise: NoiseFloor | None, *, scope: str, profile: Mapping[str, Any],
             evaluator_set_version: str, suite: str | None = None,
             evaluators: Sequence[str] | None = None,
             latency_control: Mapping[str, Any] | None = None) -> GateReport:
    """Apply rules 1-5. `scenarios` and `thresholds` are main's gate definition;
    `profile` is thresholds.profiles[<preview|staging>] (repeats); `scope` is the run scope.
    `latency_control` (evaluation/control.py `judge()`, or {"status": "SKIP", ...} when the scope has no
    happy_path scenarios) makes the p95 rule relative to a concurrent control arm; the absolute and baseline
    p95 budget then becomes the INFO user-experience line. Without it the p95 budget gates as before."""
    in_run = [s for s in scenarios if in_scope(s, scope)]
    by_id = {s["scenario_id"]: s for s in in_run}
    got = _latest(r for r in results if r.scenarioId in by_id)
    evaluator_ids = list(evaluators) if evaluators is not None else gate_evaluators(thresholds)
    gates = thresholds.get("gates") or {}
    lines: list[GateLine] = []
    reasons: list[str] = []
    suite = suite if suite is not None else suite_sha256(scenarios)

    # Rule 1: hard gates.
    for gate in gates.get("hard") or []:
        ev, pass_at = gate["evaluator"], float(gate.get("passAt", 1.0))
        targets = [s for s in in_run if matches(s, gate["scope"])]
        label = gate["scope"] if isinstance(gate["scope"], str) else ", ".join(gate["scope"])
        if not targets:
            lines.append(GateLine("hard", ev, label, SKIP, "no scenarios in scope", f"all >= {_fmt(pass_at)}"))
            continue
        failed, missing, total = [], [], 0
        for s in targets:
            for rep in range(1, repeats_for(profile, s["category"]) + 1):
                total += 1
                r = got.get((s["scenario_id"], rep, ev))
                if r is None or r.value is None:
                    missing.append(f"{s['scenario_id']}#{rep}")
                elif r.value < pass_at:
                    failed.append(f"{s['scenario_id']}#{rep}")
        status = FAIL if failed else (INCONCLUSIVE if missing else PASS)
        detail = ("failed " + ", ".join(failed[:5]) + (" ..." if len(failed) > 5 else "")) if failed else (
            ("missing " + ", ".join(missing[:5])) if missing else "")
        lines.append(GateLine("hard", ev, label, status, f"{total - len(failed) - len(missing)}/{total} pass",
                              f"all >= {_fmt(pass_at)}", detail))

    # Rule 2: statistical gates.
    for gate in gates.get("statistical") or []:
        ev = gate["evaluator"]
        cats = [gate["scope"]] if isinstance(gate["scope"], str) else list(gate["scope"])
        for cat in cats:
            targets = [s for s in in_run if matches(s, cat)]
            name = f"{ev} {cat}"
            threshold = f"mean >= {_fmt(gate['minMean'])}, regression <= {_fmt(gate['maxRegressionVsBaseline'])}"
            if not targets:
                lines.append(GateLine("statistical", name, scope, SKIP, "no scenarios in scope", threshold))
                continue
            values = [got[(s["scenario_id"], rep, ev)].value for s in targets
                      for rep in range(1, repeats_for(profile, s["category"]) + 1)
                      if got.get((s["scenario_id"], rep, ev)) is not None
                      and got[(s["scenario_id"], rep, ev)].value is not None]
            if not values:
                lines.append(GateLine("statistical", name, scope, INCONCLUSIVE, "no values", threshold))
                continue
            m = stats.mean(values)
            key = (ev, cat, scope)
            if baseline is None:
                lines.append(GateLine("statistical", name, scope, INCONCLUSIVE, f"mean {_fmt(m)}", threshold,
                                      "baseline pending"))
                continue
            if str(baseline.evaluatorSetVersion) != str(evaluator_set_version):
                lines.append(GateLine("statistical", name, scope, INCONCLUSIVE, f"mean {_fmt(m)}", threshold,
                                      "re-baseline required"))
                continue
            if key not in baseline.stats or noise is None or key not in noise.sd:
                lines.append(GateLine("statistical", name, scope, INCONCLUSIVE, f"mean {_fmt(m)}", threshold,
                                      f"baseline missing for scope {scope}"))
                continue
            base_mean = baseline.stats[key][0]
            noise_r = stats.scaled_noise(noise.sd[key], noise.r_cal, repeats_for(profile, cat))
            max_reg = float(gate["maxRegressionVsBaseline"])
            tolerance = max(max_reg, 2 * noise_r)
            tol_text = f"tolerance {tolerance:.2f}" + (f" (2σ, {scope})" if 2 * noise_r > max_reg else "")
            ok = m >= float(gate["minMean"]) and m >= base_mean - tolerance - 1e-12
            lines.append(GateLine("statistical", name, scope, PASS if ok else FAIL,
                                  f"mean {_fmt(m)} vs baseline {_fmt(base_mean)}", threshold, tol_text))

    # Rule 3: budgets.
    budget_lines = _budget_lines(thresholds.get("budgets") or {}, list(metrics), baseline, scope, noise=noise,
                                 repeats=repeats_for(profile, "default"))
    if latency_control is not None:
        for line in budget_lines:
            if line.name == "p95LatencyMs":
                outcome = "within" if line.status == PASS else ("over" if line.status == FAIL else "not compared")
                line.name, line.status = UX_BUDGET, INFO
                line.detail = f"{outcome}; informational, not gated" + (f"; {line.detail}" if line.detail else "")
        budget_lines.append(_control_line(latency_control))
    lines += budget_lines

    # Rule 4: coverage.
    expected = sum(repeats_for(profile, s["category"]) for s in in_run) * len(evaluator_ids)
    present = sum(1 for (sid, rep, ev), r in got.items()
                  if ev in evaluator_ids and r.value is not None
                  and rep <= repeats_for(profile, by_id[sid]["category"]))
    coverage = present / expected if expected else 1.0
    lines.append(GateLine("coverage", "coverage", scope, PASS if coverage >= COVERAGE_MIN else INCONCLUSIVE,
                          f"{present}/{expected} ({coverage:.0%})", f">= {COVERAGE_MIN:.0%}"))

    # Suite identity overrides everything.
    comparable = baseline is not None and baseline.suiteSha256 == suite \
        and str(baseline.evaluatorSetVersion) == str(evaluator_set_version)
    if baseline is not None and baseline.suiteSha256 != suite:
        reasons.append("suite differs from baseline")
        for line in lines:
            if line.status in (PASS, FAIL):
                line.detail = (line.detail + "; " if line.detail else "") + "suite differs from baseline"
                line.status = INCONCLUSIVE
        return GateReport(INCONCLUSIVE, lines, False, scope, reasons)

    # Rule 5: verdict.
    statuses = {line.status for line in lines}
    verdict = FAIL if FAIL in statuses else (INCONCLUSIVE if INCONCLUSIVE in statuses else PASS)
    reasons += sorted({line.detail for line in lines if line.status == INCONCLUSIVE and line.detail
                       and line.kind == "statistical"})
    return GateReport(verdict, lines, comparable, scope, reasons)


def _budget_lines(budgets: Mapping[str, Any], metrics: list[SessionMetrics], baseline: Baseline | None,
                  scope: str, noise: "NoiseFloor | None" = None, repeats: int | None = None) -> list[GateLine]:
    """Absolute limits, then the scope's baseline plus a tolerance of max(relativeToBaseline x baseline, 2 noise_r),
    where noise_r is the calibration's run-to-run SD of that budget metric scaled by sqrt(r_cal / r), the same
    calibrate-before-enforce rule as the statistical gates."""
    if not budgets:
        return []
    rel = float(budgets.get("relativeToBaseline", 0.0))
    if not metrics:
        return [GateLine("budget", "budgets", scope, INCONCLUSIVE, "no session metrics", "")]
    observed = {
        "p95LatencyMs": stats.p95([m.latencyMs for m in metrics]),
        "meanTokensPerSession": stats.mean([m.inputTokens + m.outputTokens for m in metrics]),
        "meanToolCallsPerSession": stats.mean([m.toolCalls for m in metrics]),
    }
    out = []
    for key, value in observed.items():
        if key not in budgets:
            continue
        limit = float(budgets[key])
        if value > limit:
            out.append(GateLine("budget", key, scope, FAIL, _fmt(value), f"<= {_fmt(limit)}"))
            continue
        if baseline is None:
            out.append(GateLine("budget", key, scope, INCONCLUSIVE, _fmt(value), f"<= {_fmt(limit)}", "baseline pending"))
            continue
        base = (baseline.budgets.get(scope) or {}).get(key)
        if base is None:
            out.append(GateLine("budget", key, scope, INCONCLUSIVE, _fmt(value), f"<= {_fmt(limit)}",
                                f"baseline missing for scope {scope}"))
            continue
        base_value = float(base)
        tolerance, label = base_value * rel, f"+{rel:.0%}"
        sd = ((noise.budget_sd if noise is not None else {}).get(scope) or {}).get(key)
        if sd is not None and repeats:
            noise_r = stats.scaled_noise(float(sd), noise.r_cal, repeats)
            if 2 * noise_r > tolerance:
                tolerance, label = 2 * noise_r, f"+2σ {_fmt(2 * noise_r)}, {scope}"
        rel_limit = base_value + tolerance
        status = PASS if value <= rel_limit else FAIL
        out.append(GateLine("budget", key, scope, status, _fmt(value),
                            f"<= {_fmt(limit)} and <= {_fmt(rel_limit)} (baseline {_fmt(base_value)} {label})"))
    return out


def _control_line(c: Mapping[str, Any]) -> GateLine:
    """The relative p95 rule's line (evaluation/control.py)."""
    status = str(c.get("status") or INCONCLUSIVE)
    threshold = (f"FAIL if > {_fmt(c.get('effect', 1.25))}x control and exceedance p < {_fmt(c.get('alpha', 0.01))}"
                 f" (n >= {c.get('minSessions', 20)} each)")
    if status == SKIP:
        return GateLine("budget", CONTROL_LINE, "happy_path", SKIP, c.get("reason") or "no happy_path sessions",
                        threshold)
    p95c, p95s = c.get("p95Ms"), c.get("controlP95Ms")
    observed = (f"{_fmt(p95c) if p95c is not None else 'n/a'} vs control {_fmt(p95s) if p95s is not None else 'n/a'}"
                f" ({c.get('n', 0)} vs {c.get('controlN', 0)} sessions)")
    detail = c.get("detail") or ("too few sessions in an arm" if status == INCONCLUSIVE else "")
    return GateLine("budget", CONTROL_LINE, "happy_path", status, observed, threshold, detail)


# ---------------------------------------------------------------- MR note: informational table
def informational_table(main_thresholds: Mapping[str, Any], proposed_thresholds: Mapping[str, Any] | None,
                        changed_scenarios: Sequence[str], scores: Mapping[str, Mapping[str, float]] | None = None,
                        evaluator_code_changed: bool = False) -> str:
    """The MR's proposed evaluation changes, never part of the verdict.

    `changed_scenarios`: the MR's added or changed scenario IDs; the first 12 by ID are listed
    with their 1-repeat scores under main's evaluators (`scores[scenario][evaluator]`)."""
    out = ["**Proposed evaluation changes (informational; the verdict uses main's definition)**", ""]
    if proposed_thresholds is not None and proposed_thresholds != main_thresholds:
        out += ["| Setting | main | MR |", "|---|---|---|"]
        for path, old, new in _diff_paths(main_thresholds, proposed_thresholds):
            out.append(f"| {path} | {old} | {new} |")
        out.append("")
    ordered = sorted(changed_scenarios)
    if ordered:
        run, rest = ordered[:INFORMATIONAL_MAX], ordered[INFORMATIONAL_MAX:]
        out += ["| Scenario (1 repeat) | Scores |", "|---|---|"]
        for sid in run:
            row = (scores or {}).get(sid)
            text = ", ".join(f"{ev} {_fmt(v)}" for ev, v in sorted(row.items())) if row else "not run"
            out.append(f"| {sid} | {text} |")
        if rest:
            out.append(f"+{len(rest)} more not run")
        out.append("")
    if evaluator_code_changed:
        out.append("evaluator code changed: measured at Staging by the evaluator-set procedure after merge")
    return "\n".join(out).rstrip() + "\n"


def _diff_paths(a: Any, b: Any, prefix: str = "") -> list[tuple[str, Any, Any]]:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            out += _diff_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else str(k))
        return out
    return [] if a == b else [(prefix, a, b)]
