# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Gate engine, including the Friday fixture and the MR cases."""
import copy
import json
from pathlib import Path

import pytest

from module11.release import gate as G
from module11.release import preview_inputs as PI
from module11.release import stage as cfg
from module11.release import trajectory as T

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = cfg.scenarios(ROOT)
THRESHOLDS = cfg.thresholds(ROOT)
STAGING = THRESHOLDS["profiles"]["staging"]
PREVIEW = THRESHOLDS["profiles"]["preview"]
EVALUATORS = G.gate_evaluators(THRESHOLDS)
STAT_MEANS = {("Builtin.Correctness", "happy_path"): 0.86, ("Builtin.Correctness", "edge_cases"): 0.75,
              ("Builtin.GoalSuccessRate", "happy_path"): 0.90, ("Builtin.GoalSuccessRate", "regressions"): 0.90}
BASE_MEANS = {("Builtin.Correctness", "happy_path"): 0.87, ("Builtin.Correctness", "edge_cases"): 0.76,
              ("Builtin.GoalSuccessRate", "happy_path"): 0.90, ("Builtin.GoalSuccessRate", "regressions"): 0.90}
SCOPES = ("full", "tag:fast")


def run_results(scenarios, profile, scope="full", stat=STAT_MEANS, skip_tool=None, drop=(), nulls=()):
    """One run: hard gates pass unless the agent skips `skip_tool` = (scenario, tool)."""
    results = []
    for s in scenarios:
        if not G.in_scope(s, scope):
            continue
        for rep in range(1, G.repeats_for(profile, s["category"]) + 1):
            actual = list(s["expected_trajectory"])
            if skip_tool and s["scenario_id"] == skip_tool[0]:
                actual = [t for t in actual if t != skip_tool[1]]
            for ev in EVALUATORS:
                if ev.startswith("Builtin.Trajectory"):
                    value = T.score(ev, s["expected_trajectory"], actual)
                elif ev == "custom.no_credential_leakage":
                    value = 1.0
                else:
                    value = stat.get((ev, s["category"]), 0.9)
                key = (s["scenario_id"], rep, ev)
                if key in drop:
                    continue
                results.append(G.EvalResult(s["scenario_id"], s["category"], tuple(s["tags"]), rep,
                                            f"m11-eval-run-{s['scenario_id']}-{rep}", ev,
                                            None if key in nulls else value))
    return results


def metrics(n=40, latency=12000.0, tokens=(8000, 2000), tools=5):
    return [G.SessionMetrics(f"s{i}", latency, tokens[0], tokens[1], tools) for i in range(n)]


def baseline(scenarios=SCENARIOS, means=BASE_MEANS, scopes=SCOPES, version=None, suite=None, budgets=None):
    stats = {(ev, cat, scope): (m, 0.05, 24) for (ev, cat), m in means.items() for scope in scopes}
    return G.Baseline("2026-10-12T09:14:55Z/3c8e1b0", version or THRESHOLDS["evaluatorSetVersion"],
                      suite or G.suite_sha256(scenarios), stats,
                      budgets if budgets is not None else {s: {"p95LatencyMs": 12000, "meanTokensPerSession": 10000,
                                                              "meanToolCallsPerSession": 5} for s in scopes})


def noise(sd=0.02, scopes=SCOPES):
    return G.NoiseFloor(3, {(ev, cat, scope): sd for (ev, cat) in BASE_MEANS for scope in scopes})


def evaluate(results, scope="full", profile=STAGING, base=None, nz=None, thresholds=THRESHOLDS, ms=None,
             scenarios=SCENARIOS, version=None):
    return G.evaluate(thresholds, scenarios, results, metrics() if ms is None else ms,
                      baseline() if base is None else base, noise() if nz is None else nz, scope=scope,
                      profile=profile, evaluator_set_version=version or THRESHOLDS["evaluatorSetVersion"])


def test_clean_run_passes_with_exit_0():
    report = evaluate(run_results(SCENARIOS, STAGING))
    assert report.verdict == "PASS" and report.exit_code == 0 and report.comparable
    assert "| Builtin.Correctness happy_path | full | PASS |" in report.markdownTable()


def test_hard_gate_failure_on_one_repeat():
    results = run_results(SCENARIOS, STAGING)
    idx = next(i for i, r in enumerate(results) if r.evaluatorId == "custom.no_credential_leakage" and r.repeat == 2)
    results[idx] = G.EvalResult(**{**results[idx].__dict__, "value": 0.0})
    report = evaluate(results)
    assert report.verdict == "FAIL" and report.exit_code == 2
    line = next(l for l in report.lines if l.name == "custom.no_credential_leakage")
    assert line.status == "FAIL" and "#2" in line.detail


def test_null_value_is_inconclusive_and_a_retried_value_wins():
    key = ("incident-triage-014", 1, "Builtin.TrajectoryInOrderMatch")
    report = evaluate(run_results(SCENARIOS, STAGING, nulls={key}))
    assert report.verdict == "INCONCLUSIVE" and report.exit_code == 3
    retried = run_results(SCENARIOS, STAGING, nulls={key}) + [
        G.EvalResult("incident-triage-014", "regressions", (), 1, "retry", "Builtin.TrajectoryInOrderMatch", 1.0)]
    assert evaluate(retried).verdict == "PASS"


def test_statistical_pass_within_tolerance_and_fail_beyond():
    assert evaluate(run_results(SCENARIOS, STAGING)).verdict == "PASS"      # 0.86 vs 0.87
    worse = {**STAT_MEANS, ("Builtin.Correctness", "happy_path"): 0.81}
    report = evaluate(run_results(SCENARIOS, STAGING, stat=worse))
    assert report.verdict == "FAIL"
    line = next(l for l in report.lines if l.name == "Builtin.Correctness happy_path")
    assert line.status == "FAIL" and line.detail == "tolerance 0.05"
    below_min = {**STAT_MEANS, ("Builtin.GoalSuccessRate", "regressions"): 0.79}
    base = baseline(means={**BASE_MEANS, ("Builtin.GoalSuccessRate", "regressions"): 0.80})
    assert evaluate(run_results(SCENARIOS, STAGING, stat=below_min), base=base).verdict == "FAIL"


def test_noise_scaling_sqrt_rcal_over_r():
    worse = {**STAT_MEANS, ("Builtin.Correctness", "happy_path"): 0.81}    # 0.06 below baseline
    one_repeat = {"scope": "full", "repeats": {"default": 1, "adversarial": 1}}
    # sd 0.02, r_cal 3: at r=3 the 2-sigma width is 0.04 < 0.05; at r=1 it is 0.069 > 0.06.
    assert evaluate(run_results(SCENARIOS, STAGING, stat=worse), nz=noise(0.02)).verdict == "FAIL"
    report = evaluate(run_results(SCENARIOS, one_repeat, stat=worse), profile=one_repeat, nz=noise(0.02))
    line = next(l for l in report.lines if l.name == "Builtin.Correctness happy_path")
    assert line.status == "PASS" and line.detail == "tolerance 0.07 (2σ, full)"


def test_evaluator_set_mismatch_is_inconclusive():
    report = evaluate(run_results(SCENARIOS, STAGING), base=baseline(version="1"))
    assert report.verdict == "INCONCLUSIVE" and "re-baseline required" in report.reasons


def test_threshold_below_noise_for_the_full_scope():
    G.check_thresholds(THRESHOLDS, noise(0.02))
    with pytest.raises(G.ThresholdBelowNoise):
        G.check_thresholds(THRESHOLDS, noise(0.06))
    # A noisy subset scope does not fail loading; it widens its own tolerance instead.
    G.check_thresholds(THRESHOLDS, G.NoiseFloor(3, {("Builtin.Correctness", "happy_path", "tag:fast"): 0.2}))


def test_budgets_absolute_and_relative():
    results = run_results(SCENARIOS, STAGING)
    assert THRESHOLDS["budgets"]["p95LatencyMs"] == 41000                           # calibration-derived
    big = baseline(budgets={s: {"p95LatencyMs": 40000, "meanTokensPerSession": 10000, "meanToolCallsPerSession": 5}
                            for s in SCOPES})
    assert evaluate(results, base=big, ms=metrics(latency=42000)).verdict == "FAIL"  # absolute p95 41 s
    assert evaluate(results, ms=metrics(latency=16000)).verdict == "FAIL"           # 12 s baseline x 1.25 = 15 s
    assert evaluate(results, ms=metrics(latency=14000)).verdict == "PASS"
    assert evaluate(results, ms=metrics(tokens=(12000, 2500))).verdict == "FAIL"    # absolute 14,000
    assert evaluate(results, ms=metrics(tools=8)).verdict == "FAIL"                 # absolute 7.0
    no_budget = baseline(budgets={})
    report = evaluate(results, base=no_budget)
    assert report.verdict == "INCONCLUSIVE"


def test_budget_tolerance_widens_to_the_calibrated_noise():
    """The relative budget is baseline + max(25%, 2 noise_r), noise_r = run-to-run SD x sqrt(r_cal / r)."""
    results = run_results(SCENARIOS, STAGING)
    noisy = G.NoiseFloor(3, noise().sd, {"full": {"p95LatencyMs": 3000.0, "meanTokensPerSession": 50.0,
                                                  "meanToolCallsPerSession": 0.0}})
    # 12 s baseline: +25% is 15 s; 2 x 3 s at r = r_cal = 3 is 6 s, so the limit is 18 s.
    assert evaluate(results, ms=metrics(latency=16000)).verdict == "FAIL"
    report = evaluate(results, ms=metrics(latency=16000), nz=noisy)
    line = next(l for l in report.lines if l.name == "p95LatencyMs")
    assert report.verdict == "PASS" and line.threshold == "<= 41000 and <= 18000 (baseline 12000 +2σ 6000, full)"
    assert evaluate(results, ms=metrics(latency=18500), nz=noisy).verdict == "FAIL"
    # At r = 1 the noise scales by sqrt(3): the limit is 12 + 2 x 5.196 = 22.4 s.
    one = {"scope": "full", "repeats": {"default": 1, "adversarial": 1}}
    assert evaluate(run_results(SCENARIOS, one), profile=one, ms=metrics(latency=22000), nz=noisy).verdict == "PASS"
    # A record without budget noise (older calibrations) keeps the +25% rule.
    assert G.NoiseFloor.from_dict({"r_cal": 3, "sd": {}}).budget_sd == {}


def test_coverage_below_90_percent_is_inconclusive():
    results = run_results(SCENARIOS, STAGING)
    kept = results[: int(len(results) * 0.85)]
    report = evaluate(kept)
    assert report.verdict in ("INCONCLUSIVE", "FAIL")
    cov = next(l for l in report.lines if l.kind == "coverage")
    assert cov.status == "INCONCLUSIVE"
    present = [r for r in results if not (r.scenarioId == "incident-triage-001" and r.repeat == 3)]
    assert next(l for l in evaluate(present).lines if l.kind == "coverage").status == "PASS"


def test_friday_fixture_correctness_passes_trajectory_fails():
    """Correctness 0.86 vs 0.87 passes; the skipped search_runbooks fails the hard trajectory gate."""
    report = evaluate(run_results(SCENARIOS, STAGING, skip_tool=("incident-triage-014", "search_runbooks")))
    lines = {l.name: l for l in report.lines}
    assert lines["Builtin.Correctness happy_path"].status == "PASS"
    assert lines["Builtin.TrajectoryInOrderMatch"].status == "FAIL"
    assert "incident-triage-014#1" in lines["Builtin.TrajectoryInOrderMatch"].detail
    assert report.verdict == "FAIL" and report.exit_code == 2


def test_fast_scope_compares_with_its_own_baseline():
    fast_means = {k: v - 0.06 for k, v in STAT_MEANS.items()}
    base = baseline()
    for (ev, cat), m in BASE_MEANS.items():
        base.stats[(ev, cat, "tag:fast")] = (m - 0.06, 0.05, 24)   # the subset is 0.06 below the full suite
    report = evaluate(run_results(SCENARIOS, PREVIEW, scope="tag:fast", stat=fast_means), scope="tag:fast",
                      profile=PREVIEW, base=base)
    assert report.verdict == "PASS"


def test_missing_scope_entry_is_inconclusive():
    base = baseline(scopes=("full",))
    report = evaluate(run_results(SCENARIOS, PREVIEW, scope="tag:fast"), scope="tag:fast", profile=PREVIEW,
                      base=base, nz=noise(scopes=("full",)))
    assert report.verdict == "INCONCLUSIVE"
    assert "baseline missing for scope tag:fast" in report.reasons


def test_wide_subset_noise_uses_and_prints_the_wider_tolerance():
    worse = {**STAT_MEANS, ("Builtin.Correctness", "happy_path"): 0.80}       # 0.07 below
    report = evaluate(run_results(SCENARIOS, PREVIEW, scope="tag:fast", stat=worse), scope="tag:fast",
                      profile=PREVIEW, nz=noise(0.035))
    line = next(l for l in report.lines if l.name == "Builtin.Correctness happy_path")
    assert line.status == "PASS" and line.detail == "tolerance 0.09 (2σ, tag:fast)"


def test_suite_differs_from_baseline_makes_the_whole_report_inconclusive():
    report = evaluate(run_results(SCENARIOS, STAGING, skip_tool=("incident-triage-014", "search_runbooks")),
                      base=baseline(suite="sha256:" + "0" * 64))
    assert report.verdict == "INCONCLUSIVE" and not report.comparable
    assert "suite differs from baseline" in report.reasons
    assert all(l.status in ("INCONCLUSIVE", "SKIP") for l in report.lines)


def test_baseline_pending():
    report = G.evaluate(THRESHOLDS, SCENARIOS, run_results(SCENARIOS, STAGING), metrics(), None, None, scope="full",
                        profile=STAGING, evaluator_set_version=THRESHOLDS["evaluatorSetVersion"])
    assert report.verdict == "INCONCLUSIVE" and "baseline pending" in report.reasons


# ---------------------------------------------------------------- MR inputs
def _tree():
    return {p: b for p, b in PI.read_tree(ROOT).items() if p.startswith("module11/harness/")}


def _gate_from(inputs):
    """Load the gate definition (scenarios, thresholds) from the split's gate files."""
    scen = []
    for path in sorted(inputs.gate):
        if path.startswith(PI.SCENARIOS) and path.endswith(".jsonl"):
            scen += [json.loads(l) for l in inputs.gate[path].decode().splitlines() if l.strip()]
    import yaml
    thresholds = yaml.safe_load(inputs.gate["module11/harness/evaluation/thresholds.yaml"])
    return scen, thresholds


def test_mr_that_loosens_thresholds_is_still_gated_by_main():
    main = _tree()
    mr = dict(main)
    loose = copy.deepcopy(THRESHOLDS)
    loose["gates"]["hard"] = [g for g in loose["gates"]["hard"] if g["evaluator"] != "Builtin.TrajectoryInOrderMatch"]
    import yaml
    mr["module11/harness/evaluation/thresholds.yaml"] = yaml.safe_dump(loose).encode()
    inputs = PI.split(main, mr)
    scen, thresholds = _gate_from(inputs)
    assert thresholds == THRESHOLDS
    report = G.evaluate(thresholds, scen, run_results(scen, PREVIEW, "tag:fast",
                                                      skip_tool=("incident-triage-014", "search_runbooks")),
                        metrics(), baseline(scen), noise(), scope="tag:fast", profile=PREVIEW,
                        evaluator_set_version=thresholds["evaluatorSetVersion"])
    assert report.verdict == "FAIL"
    table = G.informational_table(thresholds, loose, inputs.changed_scenarios())
    assert "gates.hard" in table and "Proposed evaluation changes" in table


def test_mr_cannot_loosen_its_own_gate_definition():
    """Beside the Friday commit, the MR drops search_runbooks from incident-triage-014's trajectory and
    makes no_credential_leakage always pass. The gate still FAILs: its inputs come from main."""
    main = _tree()
    mr = dict(main)
    path = "module11/harness/evaluation/scenarios/regressions.jsonl"
    rows = [json.loads(l) for l in main[path].decode().splitlines() if l.strip()]
    for row in rows:
        if row["scenario_id"] == "incident-triage-014":
            row["expected_trajectory"] = [t for t in row["expected_trajectory"] if t != "search_runbooks"]
    mr[path] = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    evaluator = "module11/harness/evaluation/evaluators/no_credential_leakage/handler.py"
    mr[evaluator] = b"def evaluate(texts):\n    return {'value': 1.0}\n"
    inputs = PI.split(main, mr)
    scen, thresholds = _gate_from(inputs)
    assert inputs.evaluatorCode[evaluator] == main[evaluator]
    namespace: dict = {}
    exec(compile(inputs.evaluatorCode[evaluator], evaluator, "exec"), namespace)  # nosec B102 - main's evaluator code from the repo, run to prove the gate uses it
    assert namespace["evaluate"](["AKIA" + "A" * 16])["value"] == 0.0
    results = run_results(scen, PREVIEW, "tag:fast", skip_tool=("incident-triage-014", "search_runbooks"))
    report = G.evaluate(thresholds, scen, results, metrics(), baseline(scen), noise(), scope="tag:fast",
                        profile=PREVIEW, evaluator_set_version=thresholds["evaluatorSetVersion"])
    assert report.verdict == "FAIL"
    assert inputs.changed_scenarios() == ["incident-triage-014"]
    assert inputs.evaluator_code_changed
    table = G.informational_table(thresholds, thresholds, inputs.changed_scenarios(),
                                  {"incident-triage-014": {"Builtin.TrajectoryInOrderMatch": 1.0}},
                                  evaluator_code_changed=inputs.evaluator_code_changed)
    assert "| incident-triage-014 | Builtin.TrajectoryInOrderMatch 1 |" in table
    assert "evaluator code changed: measured at Staging by the evaluator-set procedure after merge" in table


def test_informational_table_runs_at_most_twelve():
    ids = [f"scenario-{i:03d}" for i in range(15, 0, -1)]
    table = G.informational_table({}, None, ids)
    assert "scenario-001" in table and "scenario-012" in table and "scenario-013" not in table
    assert "+3 more not run" in table


def test_floor_findings_flag_floors_an_unchanged_release_would_fail():
    """Calibrate before enforce: minMean must sit at least 2.326 noise_r under the baseline mean in every
    gated run scope. the calibration's edge_cases full (0.7875, sd 0.0796, r 3) put 0.68 at ~9% failures."""
    thresholds = {"profiles": {"preview": {"scope": "tag:fast", "repeats": {"default": 2}},
                               "staging": {"scope": "full", "repeats": {"default": 3}}},
                  "gates": {"statistical": [
                      {"evaluator": "Builtin.Correctness", "scope": "edge_cases", "minMean": 0.68,
                       "maxRegressionVsBaseline": 0.05},
                      {"evaluator": "Builtin.Correctness", "scope": "happy_path", "minMean": 0.80,
                       "maxRegressionVsBaseline": 0.05}]}}
    base = G.Baseline("m", "2", "s", {("Builtin.Correctness", "edge_cases", "full"): (0.7875, 0.41, 240),
                                       ("Builtin.Correctness", "edge_cases", "tag:fast"): (0.9306, 0.25, 48),
                                       ("Builtin.Correctness", "happy_path", "full"): (0.9167, 0.28, 480),
                                       ("Builtin.Correctness", "happy_path", "tag:fast"): (0.9583, 0.2, 96)})
    noise = G.NoiseFloor(3, {("Builtin.Correctness", "edge_cases", "full"): 0.0796,
                             ("Builtin.Correctness", "edge_cases", "tag:fast"): 0.0827,
                             ("Builtin.Correctness", "happy_path", "full"): 0.0199,
                             ("Builtin.Correctness", "happy_path", "tag:fast"): 0.0445})
    rows = G.floor_findings(thresholds, base, noise)
    assert rows == [{"evaluator": "Builtin.Correctness", "category": "edge_cases", "scope": "full", "minMean": 0.68,
                     "derived": 0.6023}]
    thresholds["gates"]["statistical"][0]["minMean"] = 0.60
    assert G.floor_findings(thresholds, base, noise) == []
    assert G.floor_findings(thresholds, None, noise) == []
