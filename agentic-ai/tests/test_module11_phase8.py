# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Experiment and corpus operator tooling, offline: the drift report's
SSM form and the --external block, cost per successful session by manifest, the A/B test's variants and results,
the recommendation request and import, the shadow report and model comparison, scenario harvesting, the traffic
rate, and the demo baseline checks."""
from __future__ import annotations

import copy
import json
import re

import pytest

from module11.aws import drift_reads
from module11.cli_demo import baseline_checks
from module11.cli_ops import external_lines, parse_rate
from module11.evaluation import abtest as AB
from module11.evaluation import cost as COST
from module11.evaluation import recommendations as REC
from module11.evaluation import scenario_harvest as SHV
from module11.evaluation import shadow as SH
from module11.release import drift
from test_module11_support import ROOT


# ---------------------------------------------------------------- drift
def test_drift_report_parameter_fits_a_standard_parameter_and_keeps_counts():
    findings = [drift.Finding("endpoint", f"e{i}", 24, f"live {i}", "READY") for i in range(200)]
    report = drift.DriftReport("prod", "2026-10-08T00:00:00+00:00", findings)
    doc = drift_reads.report_parameter(report)
    assert len(json.dumps(doc, sort_keys=True, separators=(",", ":"))) <= drift_reads.MAX_REPORT_CHARS
    assert doc["driftCount"] == 200 and doc["findingCount"] == 200 and doc["truncated"] is True
    back = drift_reads.report_from({"report": report.to_dict()})
    assert back.drift_count == 200 and back.findings[0].text() == findings[0].text()


def test_external_block_labels_the_stand_in_and_flags_changes():
    probe = {"models": {"us.openai.gpt-5.6-luna": "ACTIVE"},
             "builtinFingerprint": {"pinned": "sha256:" + "a" * 64, "observed": "sha256:" + "a" * 64},
             "surfaces": [{"id": "staging-ab-gateway/companion", "pinned": "sha256:1", "observed": "sha256:1"}]}
    lines = [t for t, _ in external_lines(probe)]
    assert lines[0] == "model us.openai.gpt-5.6-luna: ACTIVE"
    assert "unchanged" in lines[1]
    assert lines[2] == "external surface staging-ab-gateway/companion: sha256 matches pin (stand-in: this Gateway target is ours)"
    probe["surfaces"][0]["observed"] = "sha256:2"
    probe["builtinFingerprint"]["pinned"] = None
    lines = [t for t, _ in external_lines(probe)]
    assert "not pinned" in lines[1] and "!= pin" in lines[2]


def test_builtin_fingerprint_is_order_independent():
    class Ctl:
        def __init__(self, rows):
            self.rows = rows

        def list_evaluators(self, **_):
            return {"evaluators": self.rows}
    a = [{"evaluatorId": "Builtin.B", "level": "TRACE"}, {"evaluatorId": "Builtin.A", "level": "SESSION"},
         {"evaluatorId": "custom-x", "level": "SESSION"}]
    assert drift_reads.builtin_fingerprint(Ctl(a)) == drift_reads.builtin_fingerprint(Ctl(list(reversed(a))))
    assert drift_reads.builtin_fingerprint(Ctl(a)) != drift_reads.builtin_fingerprint(Ctl(a[:1]))


# ---------------------------------------------------------------- cost by manifest
def _item(mv, release, status="completed", tokens=(6000, 400), tools=5, ms=12000, shadow=False, t=1000):
    return {"manifestVersion": mv, "release": release, "status": status, "inputTokens": tokens[0],
            "outputTokens": tokens[1], "toolCalls": tools, "latencyMs": ms, "shadow": shadow, "sk": f"{t}#c"}


def test_cost_per_successful_session_by_manifest():
    items = [_item("mv-a", "harness-v15", t=1_000_000) for _ in range(9)] + \
            [_item("mv-a", "harness-v15", status="failed", t=1_000_001)] + \
            [_item("mv-b", "harness-v16", tools=4, t=2_000_000) for _ in range(4)] + \
            [_item("mv-b", "harness-v16", shadow=True, t=2_000_001)]
    rows = COST.by_manifest(items, {"models": {}}, "us.openai.gpt-5.6-luna")
    assert [r.release for r in rows] == ["harness-v15", "harness-v16"]
    a, b = rows
    assert (a.sessions, a.successful, b.sessions) == (10, 9, 4) and b.tool_calls == 4.0
    per_session = (6000 / 1000 * COST.ASSUMED_MODEL_IN_PER_1K + 400 / 1000 * COST.ASSUMED_MODEL_OUT_PER_1K
                   + 12 * COST.RUNTIME_PER_SECOND)
    assert a.per_successful_usd == pytest.approx(per_session * 10 / 9)
    text = "\n".join(COST.manifest_lines(rows))
    assert "planning price" in text and "harness-v16" in text and "$ / ok session" in text


# ---------------------------------------------------------------- A/B
def test_ab_treatment_changes_only_the_companion_prompt():
    from module11.release import manifest as M
    control = M.bundle_component(ROOT)
    treatment = AB.treatment_component(control, "New prompt.\r\nSecond line.")
    assert treatment["prompts"]["companion"] == "New prompt.\nSecond line.\n"
    assert {k: v for k, v in treatment.items() if k != "prompts"} == {k: v for k, v in control.items() if k != "prompts"}
    assert treatment["prompts"]["planner"] == control["prompts"]["planner"] and control["prompts"]["companion"] != "x"
    with pytest.raises(ValueError):
        AB.treatment_component(control, "   ")
    assert AB.component_sha256(treatment) != AB.component_sha256(control)


def test_ab_variants_and_results():
    v = AB.variants("arn:b", "c1", "t1")
    assert [x["name"] for x in v] == ["C", "T1"] and [x["weight"] for x in v] == [50, 50]
    with pytest.raises(ValueError):
        AB.variants("arn:b", "c1", "t1", (60, 60))
    response = {"name": "m11_staging_ab_0123abcd", "abTestId": "id", "status": "ACTIVE", "executionStatus": "RUNNING",
                "results": {"evaluatorMetrics": [{"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate",
                                                  "controlStats": {"variantName": "C", "sampleSize": 98, "mean": 0.91},
                                                  "variantResults": [{"variantName": "T1", "sampleSize": 101, "mean": 0.95,
                                                                      "absoluteChange": 0.04, "pValue": 0.21,
                                                                      "isSignificant": False}]}]}}
    (row,) = AB.result_rows(response)
    assert (row.evaluator, row.control_n, row.treatment_mean, row.significant) == ("Builtin.GoalSuccessRate", 98, 0.95, False)
    text = "\n".join(AB.show_lines(response, {"controlVersion": "aaaaaaaa1", "treatmentVersion": "bbbbbbbb2"}))
    assert "0.910 (98)" in text and "0.950 (101)" in text and "+0.040" in text and "0.21" in text
    assert "no results yet" in "\n".join(AB.show_lines({"name": "x"}))
    assert AB.target_url("https://gw.example/", "companion") == "https://gw.example/companion"


def test_ab_traffic_counts_and_never_resends():
    calls = []

    def invoke(sid, payload):
        calls.append((sid, json.loads(payload)))
        if len(calls) % 5 == 0:
            raise RuntimeError("boom")
        return b"event: accepted\n\nevent: result\ndata: {}\n\n"
    out = AB.drive(invoke, sessions=20, concurrency=3, identity={"manifestVersion": "mv", "behaviorDigest": "sha256:x",
                                                                  "release": "harness-v16"},
                   fixtures={"payments": ["p"], "platform": ["q"]}, mix={"payments": 0.6, "platform": 0.4},
                   ab_name="m11_staging_ab_0123abcd", log=lambda _: None, seed=1)
    assert (out.sent, out.failed, out.completed) == (20, 4, 16) and len(calls) == 20
    assert len({sid for sid, _ in calls}) == 20 and all(p["context"]["variant"] == "staging" for _, p in calls)
    assert all(sid.startswith("m11-abtest-0123abcd-") for sid, _ in calls)


# ---------------------------------------------------------------- recommendations
def test_recommendation_request_import_and_branch():
    start, end = REC.window(2.0)
    req = REC.start_request("m11_rec_0123abcd", "prompt", "arn:oe", start, end)
    cfg = req["recommendationConfig"]["systemPromptRecommendationConfig"]
    assert req["type"] == "SYSTEM_PROMPT_RECOMMENDATION" and cfg["systemPrompt"] == {"text": "prompt"}
    assert cfg["agentTraces"]["onlineEvaluation"]["onlineEvaluationConfigArn"] == "arn:oe"
    assert cfg["evaluationConfig"]["evaluators"] == [{"evaluatorArn": "arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]
    with pytest.raises(ValueError):
        REC.start_request("bad", "prompt", "arn:oe", start, end)
    assert REC.import_files("A\r\nB  ") == {REC.PROMPT: b"A\nB\n"}
    assert REC.branch_for("rec-1") == "recommendation/rec-1"
    with pytest.raises(ValueError):
        REC.branch_for("../x")
    res = REC.result_of({"status": "COMPLETED", "recommendationResult": {"systemPromptRecommendationResult": {
        "recommendedSystemPrompt": "B", "explanation": "why"}}})
    assert res["prompt"] == "B" and res["explanation"] == "why"
    lines = REC.show_lines({"name": "n", "recommendationId": "r", "window": {}}, {"status": "COMPLETED",
                           "recommendationResult": {"systemPromptRecommendationResult": {"recommendedSystemPrompt": "B\n"}}}, "A\n")
    assert any(line.strip() == "-A" for line in lines) and any(line.strip() == "+B" for line in lines)


# ---------------------------------------------------------------- shadow report and model compare
def test_shadow_report_by_name_and_real_duration(tmp_path):
    (tmp_path / "x-0000000000000001.json").write_text(json.dumps({"id": "x-0000000000000001", "name": "model-migration",
                                                                  "startedAt": 1, "openedAt": 100}))
    newer = {"id": "x-0000000000000002", "name": "model-migration", "startedAt": 2, "openedAt": 1000,
             "verdictAt": 1000 + 2 * 3600 + 25 * 60, "hours": 2, "verdict": "pass", "scoredPairs": 231,
             "batchIds": {"c": "b-c", "s": "b-s"}, "deltas": {"Builtin.GoalSuccessRate": -0.01}, "stage": "prod"}
    (tmp_path / "x-0000000000000002.json").write_text(json.dumps(newer))
    doc = SH.find(tmp_path, "model-migration")
    assert doc["id"] == "x-0000000000000002" and SH.find(tmp_path, "x-0000000000000001")["openedAt"] == 100
    text = "\n".join(SH.report_lines(doc))
    assert "a shadow run of 2 h 25 min" in text and "a day or more" in text and "231 scored pairs" in text
    assert "-0.010" in text and SH.find(tmp_path, "nothing") is None


def test_model_compare_rows_and_classification():
    results = ([{"evaluatorId": "Builtin.GoalSuccessRate", "category": "happy_path", "value": 0.5, "scenarioId": "a"}] * 10
               + [{"evaluatorId": "Builtin.GoalSuccessRate", "category": "regressions", "value": 0.6, "scenarioId": "b"}] * 10
               + [{"evaluatorId": "Builtin.Correctness", "category": "edge_cases", "value": 0.8, "scenarioId": "c"}] * 10)
    baseline = {"stats": {"Builtin.GoalSuccessRate|happy_path|full": [0.95, 0.0, 24],
                          "Builtin.GoalSuccessRate|regressions|full": [1.0, 0.0, 24],
                          "Builtin.Correctness|edge_cases|full": [0.79, 0.0, 24]}}
    noise = {"r_cal": 3, "sd": {"Builtin.GoalSuccessRate|happy_path|full": 0.03,
                                "Builtin.GoalSuccessRate|regressions|full": 0.02,
                                "Builtin.Correctness|edge_cases|full": 0.08}}
    rows = SH.compare_rows(results, baseline, noise, repeats=3)
    moved = {(r["category"]): r["beyondNoise"] for r in rows}
    assert moved == {"happy_path": True, "regressions": True, "edge_cases": False}
    assert SH.classify(rows) == "broad"
    assert SH.classify([r for r in rows if r["category"] != "regressions"]) == "narrow"
    assert SH.classify([r for r in rows if r["category"] == "edge_cases"]) == "none"
    worst = SH.worst_scenarios(results)
    assert worst[0]["scenarioId"] == "a" and worst[0]["mean"] == 0.5


def test_models_override_and_branch():
    text = (ROOT / "module11/harness/agent/models.yaml").read_text()
    new = SH.models_override(text, "us.openai.gpt-6-luna", "openai.gpt-6-luna")
    assert new.count("modelId: us.openai.gpt-6-luna") == 2 and new.count("foundationModelId: openai.gpt-6-luna") == 2
    assert new.count("#") == text.count("#") and "maxOutputTokens: 650" in new
    assert SH.branch_for("us.openai.gpt-6-luna") == "model/openai.gpt-6-luna"


# ---------------------------------------------------------------- scenario harvesting
def test_scenario_harvest_redacts_and_expects_the_runbook(tmp_path):
    item = {"runtimeSessionId": "m11-prod-candidate-v16-" + "a" * 36, "team": "payments", "release": "harness-v16",
            "manifestVersion": "mv", "arm": "candidate", "toolsExecuted": ["get_deployment_history", "query_metrics",
                                                                            "get_service_logs"], "conversationId": "c1"}
    result = {"review": "Deployment d-5102 broke payments-service; contact jane.doe@example.com, account 111122223333.",
              "user_id": "00000000-0000-4000-8000-0000000000a1"}
    row, diff = SHV.scenario_from(item, result, category="regressions", account="111122223333", root=ROOT,
                                  prompt="payments-service started timing out after this morning's deploy.")
    assert row["category"] == "regressions" and row["service"] == "payments-service" and row["team"] == "payments"
    assert "search_runbooks" in row["expected_trajectory"]
    assert "111122223333" not in json.dumps(row) and "jane.doe@example.com" not in json.dumps(row)
    assert any(line.startswith("-") for line in diff)
    assert SHV.branch_for(item["runtimeSessionId"]) == SHV.branch_for(item["runtimeSessionId"])
    assert SHV.branch_for(item["runtimeSessionId"]).startswith("scenario/")
    body = SHV.with_row('{"scenario_id": "existing-001", "category": "regressions", "turns": [{"input": "q"}]}\n', row)
    assert len(body.strip().splitlines()) == 2 and SHV.with_row(body, row).count(row["scenario_id"]) == 1
    # The harvested row is a valid scenario line.
    from module11.release import stage as cfg
    folder = tmp_path / cfg.SCENARIO_DIR
    folder.mkdir(parents=True)
    for category in cfg.CATEGORIES:
        (folder / f"{category}.jsonl").write_text(body if category == "regressions" else "")
    assert [r["scenario_id"] for r in cfg.scenarios(tmp_path)][-1] == row["scenario_id"]
    with pytest.raises(ValueError):
        SHV.scenario_from(item, result, category="regressions", account="1", root=ROOT, prompt=None)


def test_task_input_comes_from_the_invoke_agent_span():
    spans = [{"name": "chat", "attributes": {"gen_ai.input.messages": "[...]"}},
             {"name": "invoke_agent devops_companion", "attributes": {"gen_ai.task.input": "checkout 502s"}}]
    assert SHV.task_input(spans) == "checkout 502s" and SHV.task_input([]) is None


# ---------------------------------------------------------------- traffic rate, demo baseline
@pytest.mark.parametrize("text,rate", [("20", 20.0), ("120/h", 2.0), ("2/min", 2.0), ("30/hour", 0.5)])
def test_parse_rate(text, rate):
    assert parse_rate(text) == pytest.approx(rate)


def test_parse_rate_bounds():
    with pytest.raises(ValueError):
        parse_rate("0")
    with pytest.raises(ValueError):
        parse_rate("1000/min")


def test_demo_baseline_checks():
    current = {"manifestVersion": "mv", "git": {"commit": "8e0f8b94ed7a24be"},
               "deployment": {"prod": {"release": "harness-v16"}}}
    pins = {"own": {"toolArns": {"a": "arn:a:1"}}, "granted": {"toolArns": ["arn:a:1"]}}
    tag = {"name": "harness/v16", "commit": {"id": "8e0f8b94ed7a24be820db", "short_id": "8e0f8b9"}}
    ok = baseline_checks(current=current, applied={"manifestVersion": "mv"}, control={"state": "PROMOTED"},
                         freeze={"state": "none"}, pins=pins, tag=tag, package_files=["change-plan.json", "manifest.json"],
                         drift_count=0)
    assert all(r[1] for r in ok), ok
    bad = baseline_checks(current=current, applied={"manifestVersion": "old"}, control={"state": "WARMING"},
                          freeze={"state": "planned"}, pins={**pins, "granted": {"toolArns": ["arn:a:1", "arn:a:0"]}},
                          tag=None, package_files=[], drift_count=2)
    assert [r[0] for r in bad if not r[1]] == ["prod release", "applied", "pins", "GitLab tag", "GitLab package", "drift"]
    assert baseline_checks(current=None, applied=None, control=None, freeze={}, pins=None, tag=None,
                           package_files=[], drift_count=None)[0][1] is False


# ---------------------------------------------------------------- one recalibration per change
def _base(stats, n=8, suite=None, version="2"):
    from module11.cli_eval import definition
    return {"manifestVersion": "mv", "evaluatorSetVersion": version, "suiteSha256": suite or definition(ROOT)["suite"],
            "nRuns": n, "calibrationId": "cal-x", "stats": stats, "budgets": {}}


def test_rederived_floors_lower_only_floors_above_their_calibrated_value():
    from module11.cli_eval import THRESHOLDS, rederived_floors
    from module11.release import stage as cfg
    # The floors in the repo move with every baseline refresh, so pin this one at the value the case is about.
    text = re.sub(r"(scope: edge_cases, minMean: )[0-9.]+", r"\g<1>0.60", (ROOT / THRESHOLDS).read_text(), count=1)
    thresholds = copy.deepcopy(cfg.thresholds(ROOT))
    for gate in thresholds["gates"]["statistical"]:
        if gate["evaluator"] == "Builtin.Correctness" and gate["scope"] == "edge_cases":
            gate["minMean"] = 0.60
    stats = {"Builtin.Correctness|edge_cases|full": [0.70, 0.0, 24], "Builtin.Correctness|edge_cases|tag:fast": [0.90, 0.0, 16],
             "Builtin.Correctness|happy_path|full": [0.95, 0.0, 24], "Builtin.Correctness|happy_path|tag:fast": [0.95, 0.0, 16]}
    noise = {"r_cal": 3, "sd": {"Builtin.Correctness|edge_cases|full": 0.06, "Builtin.Correctness|edge_cases|tag:fast": 0.06,
                                "Builtin.Correctness|happy_path|full": 0.01, "Builtin.Correctness|happy_path|tag:fast": 0.01}}
    new, notes = rederived_floors(text, thresholds, _base(stats), noise)
    assert "{evaluator: Builtin.Correctness, scope: edge_cases, minMean: 0.56," in new        # 0.70 - 2.326 x 0.06
    assert "{evaluator: Builtin.Correctness, scope: happy_path, minMean: 0.80," in new        # unchanged
    assert len(notes) == 1 and "0.6 -> 0.56" in notes[0]
    assert new.count("#") == text.count("#")


def test_baseline_change_carries_a_newer_evaluator_set(tmp_path):
    from module11.cli_eval import EVALUATORS, THRESHOLDS, baseline_change

    class GL:
        def __init__(self, evaluators):
            self.evaluators = evaluators

        def file(self, path, ref):
            if path == THRESHOLDS:
                return (ROOT / THRESHOLDS).read_bytes()
            if path == EVALUATORS:
                return self.evaluators
            return None
    same = (ROOT / EVALUATORS).read_bytes()
    files, notes = baseline_change(GL(same), ROOT, "2026-10-09T00:00:00Z/abcdef0")
    assert list(files) == [THRESHOLDS] and b'baselineManifest: "2026-10-09T00:00:00Z/abcdef0"' in files[THRESHOLDS]
    files, notes = baseline_change(GL(b'version: "1"\n'), ROOT, "2026-10-09T00:00:00Z/abcdef0")
    assert EVALUATORS in files and THRESHOLDS in files and "evaluator set 1 ->" in notes[0]


def test_pipeline_gate_reuses_a_matching_calibration(monkeypatch):
    from module11 import cli_eval
    from module11.aws import params
    thresholds = cli_eval.definition(ROOT)["thresholds"]
    mv = thresholds["baselineManifest"]
    version = str(thresholds["evaluatorSetVersion"])
    stats = {"Builtin.GoalSuccessRate|regressions|full": [1.0, 0.0, 24]}

    class Store:
        def __init__(self, base, noise):
            self.objects = {f"baselines/{mv}/{version}/baseline.json": base, f"baselines/{mv}/{version}/noise.json": noise}

        def get(self, key):
            return self.objects.get(key)
    ctx = type("Ctx", (), {"admin": type("A", (), {"client": lambda self, n: None})()})()
    monkeypatch.setattr(params, "stage_get", lambda ssm, stage, leaf: {"manifestVersion": mv})
    monkeypatch.setattr(cli_eval, "_store", lambda c: Store(_base(stats, version=version), {"r_cal": 3, "sd": {}}))
    assert "Reused, not re-run" in cli_eval.reusable_record(ctx, ROOT, "rebaseline")
    monkeypatch.setattr(cli_eval, "_store", lambda c: Store(_base(stats, n=1, version=version), {"r_cal": 3, "sd": {}}))
    assert cli_eval.reusable_record(ctx, ROOT, "rebaseline") is None                 # a single run is not a calibration
    monkeypatch.setattr(cli_eval, "_store", lambda c: Store(_base(stats, suite="sha256:other", version=version), {"r_cal": 3}))
    assert cli_eval.reusable_record(ctx, ROOT, "rebaseline") is None                 # another suite
    monkeypatch.setattr(params, "stage_get", lambda ssm, stage, leaf: {"manifestVersion": "2026-01-01T00:00:00Z/0000000"})
    assert cli_eval.reusable_record(ctx, ROOT, "rebaseline") is None                 # prod moved


def test_model_branch_starts_at_the_commit_prod_serves():
    from module11 import cli_phase8 as C

    class GL:
        def __init__(self):
            self.calls = []

        def branch(self, name):
            return {"name": name}

        def delete_branch(self, name):
            self.calls.append(("delete", name))

        def create_branch(self, name, ref):
            self.calls.append(("create", name, ref))
            return {}

        def file(self, path, ref):
            self.calls.append(("file", path, ref))
            return b"old"

        def commit_actions(self, branch, message, actions, start_branch=None):
            self.calls.append(("commit", branch, start_branch, [(a["action"], a["file_path"]) for a in actions]))
            return {"id": "e" * 40}
    gl, sha = GL(), "8" * 40
    C._branch_from(gl, "model/openai.gpt-6-luna", {"module11/harness/agent/models.yaml": b"x"}, "m", sha)
    assert gl.calls == [("delete", "model/openai.gpt-6-luna"), ("create", "model/openai.gpt-6-luna", sha),
                        ("file", "module11/harness/agent/models.yaml", sha),
                        ("commit", "model/openai.gpt-6-luna", None, [("update", "module11/harness/agent/models.yaml")])]
    with pytest.raises(ValueError):
        C._branch_from(gl, "main", {}, "m", sha)
