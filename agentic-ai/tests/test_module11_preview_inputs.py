# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MR preview inputs."""
from pathlib import Path

from module11.release import preview_inputs as PI

ROOT = Path(__file__).resolve().parents[1]
MAIN = {p: b for p, b in PI.read_tree(ROOT).items() if p.startswith(("module11/harness/", "module11/requirements",
                                                                      "module10/"))}
EVALUATOR = "module11/harness/evaluation/evaluators/no_credential_leakage/handler.py"
THRESHOLDS = "module11/harness/evaluation/thresholds.yaml"
PROMPT = "module11/harness/agent/prompts/companion.md"


def test_evaluation_only_from_tooling_everything_else_only_from_the_mr():
    mr = dict(MAIN)
    mr[PROMPT] = b"MR prompt\n"
    mr[THRESHOLDS] = b"evaluatorSetVersion: '2'\n"
    tooling = dict(MAIN)
    tooling[PROMPT] = b"main prompt\n"
    inputs = PI.split(tooling, mr)
    assert inputs.gate[THRESHOLDS] == MAIN[THRESHOLDS]
    assert inputs.harness[PROMPT] == b"MR prompt\n"
    assert not any(p.startswith(PI.EVALUATION) for p in inputs.harness)
    assert all(p.startswith(PI.EVALUATION) for p in inputs.gate)


def test_proposed_holds_exactly_the_differing_evaluation_files():
    mr = dict(MAIN)
    mr[THRESHOLDS] = b"loosened\n"
    new = "module11/harness/evaluation/scenarios/extra.jsonl"
    mr[new] = b'{"scenario_id": "new-one"}\n'
    removed = "module11/harness/evaluation/prices.yaml"
    del mr[removed]
    inputs = PI.split(MAIN, mr)
    assert set(inputs.proposed) == {THRESHOLDS, new} and inputs.removed == [removed]
    assert inputs.changed_scenarios() == ["new-one"]
    assert not inputs.evaluator_code_changed


def test_evaluator_code_is_sealed_from_the_tooling_tree():
    mr = dict(MAIN)
    mr[EVALUATOR] = b"def evaluate(texts):\n    return {'value': 1.0}\n"
    inputs = PI.split(MAIN, mr)
    assert inputs.evaluatorCode[EVALUATOR] == MAIN[EVALUATOR]
    assert inputs.gate[EVALUATOR] == MAIN[EVALUATOR] and inputs.proposed[EVALUATOR] == mr[EVALUATOR]
    assert inputs.evaluator_code_changed


def test_closure_files_come_from_the_mr_but_never_evaluation_files():
    mr = dict(MAIN)
    mr["module10/recovery.py"] = b"# MR edit\n"
    closures = [p for p in MAIN if p.startswith("module11/harness/pipeline/closures/handler-")]
    listed = any(b"module10/recovery.py" in MAIN[p] for p in closures)
    inputs = PI.split(MAIN, mr)
    if listed:
        assert inputs.harness["module10/recovery.py"] == b"# MR edit\n"
    assert "module11/requirements-runtime.txt" in inputs.harness
    sneaky = dict(mr)
    sneaky["module11/harness/pipeline/closures/handler-query_metrics.txt"] = (THRESHOLDS + "\n").encode()
    assert THRESHOLDS not in PI.split(MAIN, sneaky).harness
