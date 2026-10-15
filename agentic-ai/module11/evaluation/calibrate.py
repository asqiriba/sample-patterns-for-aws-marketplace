# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Calibration: the baseline and the noise floor.

`run()` runs the full suite `n` times (default 8) at `r` repeats (default 3) against prod `stable` while it
serves the baseline manifest, then computes BOTH records for every scope the gates use (`full`,
`tag:fast`, `tag:retrieval` and `tag:<tool>` for each Lambda tool) from the same runs:

    baseline.json  stats[evaluator|category|scope]  = [pooled mean, sd of the values, number of values]
                   budgets[scope]                   = p95 latency, mean tokens, mean tool calls (pooled sessions)
    noise.json     sd[evaluator|category|scope]     = sd of the per-run means (the run-to-run noise), r_cal = r
                   budgetSd[scope][metric]          = sd of the per-run budget values (p95 latency, tokens, tool calls)

A gate at r repeats scales the noise by sqrt(r_cal / r) (`stats.scaled_noise`). Both are stored at
`baselines/<manifestVersion>/<evaluatorSetVersion>/` and `run()` always writes them: the bucket's versioning
keeps a replaced object. Each finished run is recorded under `calibrations/<id>/runs.json`,
so an interrupted calibration resumes with the same ID instead of paying for its runs twice.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from module11.release import gate as G
from module11.release import stats

from .records import RunRecord, S3Store, baseline_prefix, calibration_key, run_key

DEFAULT_N = 8
DEFAULT_R = 3
GATE_MIN_GOAL_SUCCESS = 0.80        # the regressions category's Builtin.GoalSuccessRate must reach this (M10)
GOAL = "Builtin.GoalSuccessRate"


class CalibrationError(RuntimeError):
    pass


def scope_names(scenarios: Sequence[Mapping[str, Any]], lambda_tools: Sequence[str]) -> list[str]:
    """`full`, `tag:fast`, `tag:retrieval`, and `tag:<tool>` for each Lambda tool some scenario is tagged with."""
    tags = {t for s in scenarios for t in s.get("tags") or []}
    names = ["full"]
    for tag in ["fast", "retrieval", *sorted(lambda_tools)]:
        if tag in tags and f"tag:{tag}" not in names:
            names.append(f"tag:{tag}")
    return names


def category_names(scenarios: Sequence[Mapping[str, Any]], thresholds: Mapping[str, Any]) -> list[str]:
    """Every gate scope entry a statistical gate names, plus each category and `all`."""
    names = {"all"} | {s["category"] for s in scenarios}
    for gate in (thresholds.get("gates") or {}).get("statistical") or []:
        entries = [gate["scope"]] if isinstance(gate["scope"], str) else list(gate["scope"])
        names.update(entries)
    return sorted(names)


def _values(record: Mapping[str, Any], evaluator: str, members: set[str]) -> list[float]:
    return [float(r["value"]) for r in record["results"]
            if r["evaluatorId"] == evaluator and r["scenarioId"] in members and r.get("value") is not None]


def compute(runs: Sequence[Mapping[str, Any]], scenarios: Sequence[Mapping[str, Any]], thresholds: Mapping[str, Any],
            evaluators: Sequence[str], scopes: Sequence[str]) -> tuple[dict, dict]:
    """(stats, noise-sd, budgets) from the runs: returns ({'stats','budgets'}, {'sd'})."""
    if not runs:
        raise CalibrationError("no runs to calibrate from")
    categories = category_names(scenarios, thresholds)
    out_stats: dict[str, list] = {}
    out_sd: dict[str, float] = {}
    budgets: dict[str, dict[str, float]] = {}
    budget_sd: dict[str, dict[str, float]] = {}
    for scope in scopes:
        in_run = [s for s in scenarios if G.in_scope(s, scope)]
        for category in categories:
            members = {s["scenario_id"] for s in in_run if G.matches(s, category)}
            if not members:
                continue
            for evaluator in evaluators:
                per_run = [_values(run, evaluator, members) for run in runs]
                pooled = [v for values in per_run for v in values]
                if not pooled:
                    continue
                means = [stats.mean(values) for values in per_run if values]
                key = G.key_str((evaluator, category, scope))
                out_stats[key] = [stats.mean(pooled), stats.sd(pooled), len(pooled)]
                out_sd[key] = stats.noise_floor(means)
        ids = {s["scenario_id"] for s in in_run}
        per_run = [[s for s in run["sessions"] if s["scenarioId"] in ids and s["status"] != "invoke_error"]
                   for run in runs]
        sessions = [s for run_sessions in per_run for s in run_sessions]
        if sessions:
            budgets[scope] = _budget_metrics(sessions)
            run_values = [_budget_metrics(run_sessions) for run_sessions in per_run if run_sessions]
            if len(run_values) >= 2:
                budget_sd[scope] = {k: stats.noise_floor([v[k] for v in run_values]) for k in budgets[scope]}
    return {"stats": out_stats, "budgets": budgets}, {"sd": out_sd, "budgetSd": budget_sd}


def _budget_metrics(sessions: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    return {"p95LatencyMs": stats.p95([float(s["latencyMs"]) for s in sessions]),
            "meanTokensPerSession": stats.mean([s["inputTokens"] + s["outputTokens"] for s in sessions]),
            "meanToolCallsPerSession": stats.mean([float(s["toolCalls"]) for s in sessions])}


def goal_success(baseline: Mapping[str, Any], category: str = "regressions", scope: str = "full") -> float | None:
    entry = (baseline.get("stats") or {}).get(G.key_str((GOAL, category, scope)))
    return None if entry is None else float(entry[0])


@dataclass
class Calibration:
    calibration_id: str
    manifest_version: str
    evaluator_set_version: str
    baseline: dict
    noise: dict
    run_ids: list[str]

    @property
    def baseline_key(self) -> str:
        return f"{baseline_prefix(self.manifest_version, self.evaluator_set_version)}/baseline.json"

    @property
    def noise_key(self) -> str:
        return f"{baseline_prefix(self.manifest_version, self.evaluator_set_version)}/noise.json"


def run(*, run_one: Callable[[int], RunRecord], store: S3Store, manifest_version: str, evaluator_set_version: str,
        suite: str, scenarios: Sequence[Mapping[str, Any]], thresholds: Mapping[str, Any], evaluators: Sequence[str],
        lambda_tools: Sequence[str], n: int = DEFAULT_N, r: int = DEFAULT_R, calibration_id: str | None = None,
        log: Callable[[str], None] = print, clock: Callable[[], float] = time.time) -> Calibration:
    """`run_one(index)` performs one full-suite run against prod stable and returns its RunRecord (the CLI builds
    it with `runner.run_suite`). Finished runs are written to `evaluations/prod/<runId>.json` as they complete."""
    if n < 2:
        raise CalibrationError("a noise floor needs at least two runs")
    calibration_id = calibration_id or time.strftime("cal-%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    index_key = calibration_key(calibration_id)
    index = store.get(index_key) or {"calibrationId": calibration_id, "manifestVersion": manifest_version,
                                     "evaluatorSetVersion": evaluator_set_version, "n": n, "r": r, "runs": []}
    for field_, want in (("manifestVersion", manifest_version), ("evaluatorSetVersion", evaluator_set_version),
                         ("r", r)):
        if index.get(field_) != want:
            raise CalibrationError(f"calibration {calibration_id} was started with {field_}={index.get(field_)!r}, "
                                   f"not {want!r}")
    records: list[dict] = []
    for entry in index["runs"]:
        doc = store.get(entry["key"])
        if doc is None:
            raise CalibrationError(f"run record {entry['key']} is missing")
        records.append(doc)
    if records:
        log(f"resuming calibration {calibration_id}: {len(records)} of {n} runs already recorded")
    while len(records) < n:
        number = len(records) + 1
        log(f"calibration {calibration_id}: run {number} of {n}")
        record = run_one(number)
        if record.suiteSha256 != suite:
            raise CalibrationError("a run measured a different suite than the calibration's")
        if record.manifestVersion != manifest_version:
            raise CalibrationError(f"prod moved to {record.manifestVersion} during the calibration "
                                   f"(expected {manifest_version}); start a new one")
        record.extra = {**record.extra, "calibrationId": calibration_id, "runNumber": number}
        key = run_key(record.stage, record.runId)
        store.put(key, record.to_dict())
        index["runs"].append({"runId": record.runId, "key": key})
        store.put(index_key, index)
        records.append(record.to_dict())
    body, noise_body = compute(records, scenarios, thresholds, evaluators, scope_names(scenarios, lambda_tools))
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))
    base = {"schema": "m11.baseline/1", "manifestVersion": manifest_version, "evaluatorSetVersion": evaluator_set_version,
            "suiteSha256": suite, "calibrationId": calibration_id, "nRuns": len(records), "rCal": r,
            "createdAt": created, "runIds": [e["runId"] for e in index["runs"]], **body}
    noise = {"schema": "m11.noise/1", "manifestVersion": manifest_version, "evaluatorSetVersion": evaluator_set_version,
             "calibrationId": calibration_id, "nRuns": len(records), "r_cal": r, "createdAt": created, **noise_body}
    result = Calibration(calibration_id, manifest_version, evaluator_set_version, base, noise,
                         [e["runId"] for e in index["runs"]])
    store.put(result.baseline_key, base)
    store.put(result.noise_key, noise)
    log(f"wrote {store.uri(result.baseline_key)} and noise.json ({len(records)} runs, r_cal {r})")
    return result


def summary_lines(baseline: Mapping[str, Any], noise: Mapping[str, Any] | None = None) -> list[str]:
    """`calibration show`: runs, r_cal and the gate-relevant numbers."""
    goal = goal_success(baseline)
    out = [f"manifest        {baseline['manifestVersion']}",
           f"evaluator set   {baseline['evaluatorSetVersion']}",
           f"calibration     {baseline.get('calibrationId')}  created {baseline.get('createdAt')}",
           f"runs            {baseline['nRuns']}   r_cal {baseline['rCal']}   suite {baseline['suiteSha256'][:19]}…"]
    for (evaluator, category) in (("Builtin.GoalSuccessRate", "regressions"), ("Builtin.GoalSuccessRate", "happy_path"),
                                  ("Builtin.Correctness", "happy_path"), ("Builtin.Correctness", "edge_cases")):
        key = G.key_str((evaluator, category, "full"))
        entry = (baseline.get("stats") or {}).get(key)
        if entry:
            sd = (noise or {}).get("sd", {}).get(key)
            out.append(f"{evaluator:<28} {category:<12} mean {entry[0]:.3f}  value sd {entry[1]:.3f}  n {entry[2]}"
                       + (f"  run-to-run sd {sd:.4f}" if sd is not None else ""))
    budget = (baseline.get("budgets") or {}).get("full")
    if budget:
        out.append("budgets (full)  " + ", ".join(f"{k} {v:.1f}" for k, v in budget.items()))
    scopes = sorted({k.rsplit("|", 1)[-1] for k in baseline.get("stats") or {}})
    out.append("scopes          " + ", ".join(scopes))
    if goal is not None:
        verdict = "ok" if goal >= GATE_MIN_GOAL_SUCCESS else f"BELOW {GATE_MIN_GOAL_SUCCESS:.2f}"
        out.append(f"regressions GoalSuccessRate {goal:.3f} ({verdict})")
    return out
