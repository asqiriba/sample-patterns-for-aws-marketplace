# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Calibration, the evaluator-set procedure and the cost projection, on the real
scenario suite and thresholds with synthetic run records. No AWS."""
import json
from pathlib import Path

import pytest

from module11.evaluation import calibrate as CAL
from module11.evaluation import cost as COST
from module11.evaluation import evalset as ES
from module11.evaluation.records import RunRecord, baseline_prefix, run_key
from module11.release import gate as G
from module11.release import stage as cfg
from module11.release import stats

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = cfg.thresholds(ROOT)
SCENARIOS = cfg.scenarios(ROOT)
EVALUATORS = cfg.evaluators(ROOT)
IDS = [e["id"] for e in EVALUATORS["evaluators"]]
SUITE = G.suite_sha256(SCENARIOS)
MV = "2026-10-06T12:58:33Z/1a2b3c4"
TOOLS = ["check_rollback_readiness", "get_blast_radius", "get_deployment_history", "get_service_logs", "query_metrics",
         "record_incident_note", "search_runbooks"]


class MemStore:
    def __init__(self):
        self.docs = {}

    def put(self, key, doc):
        self.docs[key] = json.loads(json.dumps(doc, default=str))
        return self.uri(key)

    def get(self, key):
        return self.docs.get(key)

    def exists(self, key):
        return key in self.docs

    def uri(self, key):
        return f"s3://bucket/{key}"


def synth(index, *, repeats=None, correctness=0.80, goal=0.95, trajectory=None, manifest=MV, suite=SUITE, latency=20000.0,
          run_id=None):
    """A RunRecord whose Correctness mean is `correctness + 0.01 * index` (so run means differ by 0.01)."""
    repeats = repeats or {"default": 3}
    trajectory = trajectory or {}
    results, sessions = [], []
    for s in SCENARIOS:
        for r in range(1, int(repeats.get(s["category"], repeats["default"])) + 1):
            sid = f"m11-eval-run{index:02d}-{s['scenario_id']}-{r}-abcdef12"
            sessions.append({"scenarioId": s["scenario_id"], "sessionId": sid, "status": "completed", "latencyMs": latency,
                             "inputTokens": 8000, "outputTokens": 1000, "toolCalls": 5})
            for ev in IDS:
                value = {"Builtin.Correctness": correctness + 0.01 * index, "Builtin.GoalSuccessRate": goal}.get(ev, 1.0)
                value = trajectory.get((s["scenario_id"], ev), value)
                results.append({"scenarioId": s["scenario_id"], "category": s["category"], "tags": s.get("tags", []),
                                "repeat": r, "sessionId": sid, "evaluatorId": ev, "value": value, "label": "x",
                                "errorCode": None})
    return RunRecord(run_id or f"run{index:02d}", "calibration", "prod", "stable", "calibration", "full", dict(repeats),
                     manifest, "sha256:" + "a" * 64, "operator-1a2b3c4", "2", suite, IDS, sessions, results)


def runs(n=8):
    return [synth(i).to_dict() for i in range(1, n + 1)]


SCOPES = CAL.scope_names(SCENARIOS, TOOLS)


def test_scopes_are_full_fast_retrieval_and_each_lambda_tool():
    assert SCOPES[:3] == ["full", "tag:fast", "tag:retrieval"]
    assert set(SCOPES) >= {"tag:get_deployment_history", "tag:query_metrics", "tag:search_runbooks"}
    assert "tag:planner" not in SCOPES and "tag:safety-order" not in SCOPES


def test_baseline_is_the_pooled_mean_and_noise_is_the_sd_of_the_run_means():
    body, noise = CAL.compute(runs(), SCENARIOS, THRESHOLDS, IDS, SCOPES)
    key = G.key_str(("Builtin.Correctness", "happy_path", "full"))
    mean, sd_values, n = body["stats"][key]
    assert mean == pytest.approx(0.80 + 0.01 * 4.5) and n == 8 * 20 * 3
    assert noise["sd"][key] == pytest.approx(stats.sd([0.80 + 0.01 * i for i in range(1, 9)]))
    assert body["stats"][G.key_str(("Builtin.GoalSuccessRate", "regressions", "full"))][0] == pytest.approx(0.95)
    # the same 8 runs give every scope its own numbers, restricted to that scope's scenarios
    fast = body["stats"][G.key_str(("Builtin.Correctness", "happy_path", "tag:fast"))]
    assert fast[2] == 8 * 3 * sum(1 for s in SCENARIOS if s["category"] == "happy_path" and "fast" in s["tags"])
    assert G.key_str(("Builtin.Correctness", "edge_cases", "tag:query_metrics")) in body["stats"]
    assert set(body["budgets"]) == set(SCOPES)
    assert body["budgets"]["full"] == {"p95LatencyMs": 20000.0, "meanTokensPerSession": 9000.0, "meanToolCallsPerSession": 5.0}
    # budget noise: the SD of the per-run budget values; the synthetic runs are identical there
    assert set(noise["budgetSd"]) == set(SCOPES)
    assert noise["budgetSd"]["full"] == {"p95LatencyMs": 0.0, "meanTokensPerSession": 0.0, "meanToolCallsPerSession": 0.0}
    G.NoiseFloor.from_dict({"r_cal": 3, **noise})                       # the gate reads what calibrate writes


def test_a_single_run_has_zero_noise_but_calibrate_refuses_fewer_than_two():
    _, noise = CAL.compute(runs(1), SCENARIOS, THRESHOLDS, IDS, SCOPES)
    assert set(noise["sd"].values()) == {0.0}
    with pytest.raises(CAL.CalibrationError):
        CAL.run(run_one=lambda n: synth(n), store=MemStore(), manifest_version=MV, evaluator_set_version="2", suite=SUITE,
                scenarios=SCENARIOS, thresholds=THRESHOLDS, evaluators=IDS, lambda_tools=TOOLS, n=1, log=lambda l: None)


def calibrate(store=None, n=8, run_one=None, calibration_id="cal-test", **over):
    store = store or MemStore()
    kw = dict(run_one=run_one or (lambda number: synth(number)), store=store, manifest_version=MV,
              evaluator_set_version="2", suite=SUITE, scenarios=SCENARIOS, thresholds=THRESHOLDS, evaluators=IDS,
              lambda_tools=TOOLS, n=n, r=3, calibration_id=calibration_id, log=lambda line: None)
    kw.update(over)
    return CAL.run(**kw), store


def test_calibrate_writes_runs_baseline_and_noise_at_the_design_keys():
    result, store = calibrate()
    prefix = baseline_prefix(MV, "2")
    assert prefix == f"baselines/{MV}/2"
    assert store.exists(f"{prefix}/baseline.json") and store.exists(f"{prefix}/noise.json")
    assert [k for k in store.docs if k.startswith("evaluations/prod/")] == [run_key("prod", f"run{i:02d}") for i in range(1, 9)]
    base = store.get(f"{prefix}/baseline.json")
    assert base["nRuns"] == 8 and base["rCal"] == 3 and base["suiteSha256"] == SUITE and base["manifestVersion"] == MV
    assert store.get(f"{prefix}/noise.json")["r_cal"] == 3
    assert store.get("evaluations/prod/run03.json")["extra"] == {"calibrationId": "cal-test", "runNumber": 3}
    text = "\n".join(CAL.summary_lines(base, store.get(f"{prefix}/noise.json")))
    assert "runs            8   r_cal 3" in text and "regressions GoalSuccessRate 0.950 (ok)" in text


def test_the_written_records_load_into_the_gate_engine_and_a_matching_run_passes():
    _, store = calibrate()
    prefix = baseline_prefix(MV, "2")
    baseline = G.Baseline.from_dict(store.get(f"{prefix}/baseline.json"))
    noise = G.NoiseFloor.from_dict(store.get(f"{prefix}/noise.json"))
    profile = THRESHOLDS["profiles"]["staging"]
    candidate = synth(4, repeats=profile["repeats"])                   # a run at the calibration's own mean
    report = G.evaluate(THRESHOLDS, SCENARIOS, candidate.eval_results(), candidate.metrics(), baseline, noise,
                        scope="full", profile=profile, evaluator_set_version="2", suite=SUITE,
                        evaluators=G.gate_evaluators(THRESHOLDS))
    assert report.verdict == "PASS", [l for l in report.lines if l.status != "PASS"]


def test_the_friday_edit_fails_the_trajectory_gate_on_the_calibrated_baseline():
    """The seeded regression offline: incident-triage-014 stops calling search_runbooks."""
    _, store = calibrate()
    prefix = baseline_prefix(MV, "2")
    baseline = G.Baseline.from_dict(store.get(f"{prefix}/baseline.json"))
    noise = G.NoiseFloor.from_dict(store.get(f"{prefix}/noise.json"))
    profile = THRESHOLDS["profiles"]["preview"]
    friday = synth(4, repeats=profile["repeats"], trajectory={("incident-triage-014", "Builtin.TrajectoryInOrderMatch"): 0.0})
    fast = [s for s in SCENARIOS if "fast" in s["tags"]]
    report = G.evaluate(THRESHOLDS, SCENARIOS, friday.eval_results(), friday.metrics(), baseline, noise,
                        scope="tag:fast", profile=profile, evaluator_set_version="2", suite=SUITE,
                        evaluators=G.gate_evaluators(THRESHOLDS))
    assert len(fast) == 12 and report.verdict == "FAIL" and report.exit_code == 2
    failed = [l for l in report.lines if l.status == "FAIL"]
    assert [l.name for l in failed] == ["Builtin.TrajectoryInOrderMatch"] and "incident-triage-014" in failed[0].detail
    correctness = [l for l in report.lines if l.name.startswith("Builtin.Correctness")]
    assert correctness and all(l.status == "PASS" for l in correctness)           # within tolerance


def test_an_interrupted_calibration_resumes_without_paying_for_finished_runs():
    store, calls = MemStore(), []

    def flaky(number):
        calls.append(number)
        if number == 4 and calls.count(4) == 1:
            raise RuntimeError("laptop slept")
        return synth(number)
    with pytest.raises(RuntimeError):
        calibrate(store, run_one=flaky)
    assert calls == [1, 2, 3, 4] and len(store.get("calibrations/cal-test/runs.json")["runs"]) == 3
    result, _ = calibrate(store, run_one=flaky)
    assert calls == [1, 2, 3, 4, 4, 5, 6, 7, 8] and result.baseline["nRuns"] == 8
    with pytest.raises(CAL.CalibrationError, match="r=|started with"):
        calibrate(store, r=5, run_one=flaky)


def test_calibration_refuses_a_moved_prod_or_a_different_suite():
    with pytest.raises(CAL.CalibrationError, match="prod moved"):
        calibrate(run_one=lambda n: synth(n, manifest="2026-10-07T00:00:00Z/0000000"))
    with pytest.raises(CAL.CalibrationError, match="different suite"):
        calibrate(run_one=lambda n: synth(n, suite="sha256:" + "0" * 64))


def test_goal_success_floor_is_read_from_the_regressions_category():
    result, _ = calibrate(run_one=lambda n: synth(n, goal=0.70))
    assert CAL.goal_success(result.baseline) == pytest.approx(0.70)
    assert CAL.goal_success(result.baseline) < CAL.GATE_MIN_GOAL_SUCCESS
    assert "BELOW 0.80" in "\n".join(CAL.summary_lines(result.baseline))


# ---------------------------------------------------------------- evaluator-set change
OLD = {"version": "2", "evaluators": [{"id": "Builtin.GoalSuccessRate", "level": "session", "kind": "builtin"},
                                      {"id": "Builtin.Helpfulness", "level": "trace", "kind": "builtin"}]}
NEW = {"version": "3", "evaluators": [{"id": "Builtin.GoalSuccessRate", "level": "session", "kind": "builtin"},
                                      {"id": "custom.no_credential_leakage", "level": "session", "kind": "code"}]}


class Freeze:
    def __init__(self, state=None):
        self.doc = state or {"state": "none"}
        self.history = []

    def get(self):
        return dict(self.doc)

    def set(self, doc):
        self.doc = dict(doc)
        self.history.append(dict(doc))


def rebaseline(freeze, store=None, *, thresholds_version="3", run=None, root=None):
    store = store or MemStore()
    thresholds = {**THRESHOLDS, "evaluatorSetVersion": thresholds_version}

    def default_run(entries):
        record = synth(1)
        record.results = [dict(r, evaluatorId=e["id"], value=0.5 if e["id"] == "Builtin.Helpfulness" else 1.0)
                          for r in record.results[:5] for e in entries]
        record.evaluatorSetVersion = "3"
        return record
    return ES.rebaseline(OLD, NEW, thresholds=thresholds, get_freeze=freeze.get, set_freeze=freeze.set,
                         run_suite=run or default_run, store=store, manifest_version=MV, suite=SUITE,
                         scenarios=SCENARIOS, evaluators=["Builtin.GoalSuccessRate", "custom.no_credential_leakage"],
                         lambda_tools=TOOLS, by="arn:aws:iam::1:user/op", root=root, log=lambda l: None), store


def test_rebaseline_runs_the_six_steps(tmp_path):
    freeze = Freeze()
    change, store = rebaseline(freeze, root=tmp_path)
    assert freeze.history[0] == {**freeze.history[0], "state": "planned", "reason": "evalset 2-3"}
    assert freeze.doc == {"state": "none"}                                          # step 6: ours, cleared
    assert change["added"] == ["custom.no_credential_leakage"] and change["removed"] == ["Builtin.Helpfulness"]
    assert change["baselineWritten"] is True and store.exists(f"baselines/{MV}/3/baseline.json")
    assert store.get(f"baselines/{MV}/3/baseline.json")["nRuns"] == 1 and not store.exists(f"baselines/{MV}/3/noise.json")
    assert store.exists("evaluations/prod/run01.json")
    written = json.loads((tmp_path / "module11/harness/evaluation/evalset-changes/2-3.json").read_text())
    assert written["from"] == "2" and written["to"] == "3" and set(written["categoryDeltas"]) == {"happy_path"}
    delta = change["categoryDeltas"]["happy_path"]
    assert delta["new"] == 1.0 and delta["old"] == pytest.approx(0.75) and delta["delta"] == pytest.approx(0.25)


def test_rebaseline_never_overwrites_a_calibration_record():
    store = MemStore()
    store.put(f"baselines/{MV}/3/baseline.json", {"nRuns": 8, "marker": "calibration"})
    change, _ = rebaseline(Freeze(), store)
    assert change["baselineWritten"] is False and store.get(f"baselines/{MV}/3/baseline.json")["marker"] == "calibration"


@pytest.mark.parametrize("held", [{"state": "planned", "reason": "experiment x-1234", "by": "someone"},
                                  {"state": "emergency", "reason": "incident"}])
def test_rebaseline_refuses_a_freeze_it_does_not_own_and_leaves_it_alone(held):
    freeze = Freeze(held)
    with pytest.raises(ES.FreezeHeld) as exc:
        rebaseline(freeze)
    assert exc.value.freeze["reason"] == held["reason"] and freeze.history == [] and freeze.doc == held


def test_rebaseline_requires_the_threshold_edit_and_sets_no_freeze_without_it():
    freeze = Freeze()
    with pytest.raises(ES.EvalSetMismatch):
        rebaseline(freeze, thresholds_version="2")
    assert freeze.history == []
    with pytest.raises(ES.EvalSetMismatch):
        ES.rebaseline(NEW, NEW, thresholds=THRESHOLDS, get_freeze=freeze.get, set_freeze=freeze.set,
                      run_suite=lambda e: None, store=MemStore(), manifest_version=MV, suite=SUITE, scenarios=SCENARIOS,
                      evaluators=[], lambda_tools=TOOLS, by="x")


def test_rebaseline_clears_its_own_freeze_when_the_run_fails_but_not_someone_elses():
    freeze = Freeze()

    def boom(entries):
        raise RuntimeError("model quota")
    with pytest.raises(RuntimeError):
        rebaseline(freeze, run=boom)
    assert freeze.doc == {"state": "none"} and freeze.history[0]["state"] == "planned"

    class Stolen(Freeze):
        def set(self, doc):
            super().set(doc)
            self.doc = {"state": "planned", "reason": "experiment x-9999"} if doc.get("state") == "planned" else self.doc
    stolen = Stolen()
    with pytest.raises(RuntimeError):
        rebaseline(stolen, run=boom)
    assert stolen.doc["reason"] == "experiment x-9999"                              # not ours: untouched


# ---------------------------------------------------------------- cost
def test_projection_counts_sessions_from_the_profiles():
    staging = COST.project("staging", SCENARIOS, THRESHOLDS["profiles"]["staging"]["repeats"], evaluators=IDS)
    fast = [s for s in SCENARIOS if "fast" in s["tags"]]
    preview = COST.project("preview", fast, THRESHOLDS["profiles"]["preview"]["repeats"], evaluators=IDS)
    calibration = COST.project("calibration", SCENARIOS, {"default": 3}, evaluators=IDS)
    assert (staging.sessions, preview.sessions, calibration.sessions) == (136, 24, 126)
    judged = len([e for e in IDS if e in COST.JUDGED])          # 2 in evaluator set v2, 3 in v3 (+ Helpfulness)
    assert staging.invocations == staging.sessions + 2 * 3 and staging.judge_evaluations == judged * 136
    assert staging.code_evaluations == 136 and 10 < staging.total_usd < 40
    assert any("prices.yaml holds no price" in a for a in staging.assumptions)
    assert calibration.total_usd * 8 < 200                                          # inside the pre-approved envelope


def test_measured_cost_replaces_the_assumptions():
    record = synth(1).to_dict()
    for r in record["results"]:
        r.update(inputTokens=6000 if r["evaluatorId"] in COST.JUDGED else 0, outputTokens=300 if r["evaluatorId"] in COST.JUDGED else 0)
    got = COST.measure(record, {"models": {"m": {"inputPer1kTokens": 0.001, "outputPer1kTokens": 0.004}}}, "m")
    assert got["modelPriceFromPricesYaml"] is True and got["sessions"] == len(record["sessions"])
    judged = len({r["evaluatorId"] for r in record["results"] if r["evaluatorId"] in COST.JUDGED})
    assert got["judgeCalls"] == judged * len(record["sessions"]) and got["codeCalls"] == len(record["sessions"])
    assert got["totalUsd"] == pytest.approx(got["modelUsd"] + got["judgeUsd"] + got["codeUsd"] + got["runtimeUsd"], abs=1e-3)
    again = COST.project("x", SCENARIOS[:3], {"default": 1}, evaluators=IDS, measured=got)
    assert any("averages measured" in a for a in again.assumptions)
