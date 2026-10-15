# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Evaluation live checks, opt-in with M11_LIVE_TESTS=1, against a deployed stage (M11_LIVE_STAGE, default
dev): test_live_spans, test_live_evaluate and test_live_stop_session. Each invokes a real scenario as the stage's
eval-runner role, waits for spans with the runner's own readiness rule, and scores them with the real Evaluations
service. Run from agentic-ai/ after `source module11/.local/env.sh`. Nothing secret is printed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("M11_LIVE_TESTS") != "1", reason="live tests need M11_LIVE_TESTS=1")

ROOT = Path(__file__).resolve().parents[1]
STAGE = os.environ.get("M11_LIVE_STAGE", "dev")
SCENARIO = "incident-triage-014"
SMALL = "incident-triage-006"                    # history and logs: a short, dependable scenario


@pytest.fixture(scope="module")
def world():
    from module11 import cli_eval
    from module11.cli_live import _context
    from module11.evaluation import runner as R
    ctx = _context(ROOT)
    d = cli_eval.definition(ROOT)
    target = cli_eval.target_of(ctx, STAGE)
    return SimpleNamespace(ctx=ctx, d=d, target=target, clients=cli_eval.clients_of(ctx, STAGE),
                           specs=R.evaluator_specs(d["evaluators"], target.evaluator_ids),
                           by_id={s["scenario_id"]: s for s in d["scenarios"]})


def run_one(world, scenario_id):
    """Invoke one scenario (session, stop), wait until its spans are ready, fetch them. -> (run, record, spans, ready_at)"""
    from module11.aws import spans as SP
    from module11.evaluation import runner as R
    scenario = world.by_id[scenario_id]
    run = R.SuiteRun(world.clients, world.target, [scenario], repeats={"default": 1}, evaluators=world.specs,
                     profile="live-test", scope="full", evaluator_set_version=str(world.d["evaluators"]["version"]),
                     suite=world.d["suite"], kind="test", log=print)
    record = run.run_session(scenario, 1)
    assert record.status == "completed", (record.status, record.detail)
    prefix = f"m11-eval-{run.run_id}-"
    ready, missing = SP.wait_for_spans(
        lambda: SP.readiness_counts(world.clients.logs, world.target.log_group, prefix, record.startedAt - 60,
                                    time.time()),
        [record.sessionId], last_invoke_at=record.endedAt, log=print)
    assert not missing, f"no spans for {record.sessionId} within 6 minutes of the last invocation"
    ready_at = time.time()
    spans = SP.session_spans(record.sessionId, world.target.log_group, record.startedAt, record.endedAt,
                             logs=world.clients.logs)
    return run, record, spans, ready_at


def test_live_spans(world):
    """One invocation, then `session_spans()` returns a list that `Evaluate` accepts for a session-level and a
    trace-level evaluator. Records the parent context the container saw."""
    from module11.aws import evaluations as EV
    from module11.evaluation.runner import trace_ids_of
    run, record, spans, _ = run_one(world, SCENARIO)
    assert spans and all(s["attributes"]["session.id"] == record.sessionId and s["scope"]["name"] for s in spans)
    names = [str(s.get("name")) for s in spans]
    assert any(n.startswith("invoke_agent") for n in names) and any(n == "execute_tool" or n.startswith("execute_tool")
                                                                   for n in names)
    traces = trace_ids_of(spans)
    assert len(traces) == 1
    scenario = world.by_id[SCENARIO]
    goal = EV.evaluate(world.clients.dp, EV.request_for(
        "Builtin.GoalSuccessRate", spans, level="session", refs=EV.references("Builtin.GoalSuccessRate", scenario,
                                                                              record.sessionId)))
    correct = EV.evaluate(world.clients.dp, EV.request_for(
        "Builtin.Correctness", spans, level="trace", trace_ids=traces,
        refs=EV.references("Builtin.Correctness", scenario, record.sessionId, traces[0])))
    assert goal.ok and 0.0 <= goal.value <= 1.0, goal
    assert correct.ok and 0.0 <= correct.value <= 1.0, correct
    root = next(s for s in spans if str(s.get("name")).startswith("invoke_agent") and s.get("traceId") == traces[0])
    print("U-65b:", json.dumps({"spans": len(spans), "rootHasParentSpanId": bool(root.get("parentSpanId")),
                                 "rootParentSpanId": root.get("parentSpanId"), "traceId": traces[0],
                                 "goal": goal.value, "correctness": correct.value}))


def test_live_evaluate(world):
    """One scenario, then `Evaluate` with all three ground-truth forms (assertions, expected response, expected
    trajectory); the service's trajectory score equals the offline matcher's; the code-based evaluator scores the
    session for the eval-runner role, and NOT for a session of that role that lacks lambda:InvokeFunction, which
    records how `Evaluate` reaches the Lambda (U-12c): with the caller's credentials."""
    from module11.aws import evaluations as EV
    from module11.aws import operator
    from module11.cli_eval import clients_of
    from module11.release import naming, trajectory
    from module11.evaluation.runner import trace_ids_of
    run, record, spans, _ = run_one(world, SCENARIO)
    scenario = world.by_id[SCENARIO]
    traces = trace_ids_of(spans)
    dp = world.clients.dp
    outcomes = {}
    for spec in world.specs:
        if spec.kind != "builtin":
            continue
        last = traces[-1]
        refs = EV.references(spec.id, scenario, record.sessionId, last)
        assert refs, f"{spec.id} has no ground truth form"
        outcomes[spec.id] = EV.evaluate(dp, EV.request_for(spec.id, spans, level=spec.level,
                                                           trace_ids=[last] if spec.level == "trace" else None, refs=refs))
    forms = {"assertions": "Builtin.GoalSuccessRate", "expectedResponse": "Builtin.Correctness",
             "expectedTrajectory": "Builtin.TrajectoryInOrderMatch"}
    for form, evaluator in forms.items():
        out = outcomes[evaluator]
        assert out.ok, (evaluator, out)
        assert not out.results[0].get("ignoredReferenceInputFields"), (form, out.results[0])
    # The service reads the same tool order as the offline twin (release/trajectory.py).
    actual = trajectory.tool_sequence(spans)
    for evaluator in ("Builtin.TrajectoryInOrderMatch", "Builtin.TrajectoryExactOrderMatch"):
        assert outcomes[evaluator].value == trajectory.score(evaluator, scenario["expected_trajectory"], actual), \
            (evaluator, actual, scenario["expected_trajectory"])
    # U-12c: the code-based evaluator runs for the eval-runner role...
    code_spec = next(s for s in world.specs if s.kind == "code")
    leak = EV.evaluate_once(dp, EV.request_for(code_spec.real_id, spans, level="session"))
    assert leak.ok and leak.value == 1.0 and leak.label == "CLEAN", leak
    # ...and fails for a session of the same role whose session policy allows Evaluate only.
    policy = json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "bedrock-agentcore:Evaluate", "Resource": "*"}]})
    narrow = operator.assume(world.ctx.role_arn(naming.eval_runner_role(STAGE)), sts=world.ctx.admin.client("sts"),
                             duration_seconds=3600, purpose="test_live_evaluate U-12c", stage=STAGE, policy=policy)
    from module11.evaluation.runner import Clients
    denied = EV.evaluate_once(Clients.from_session(narrow, world.ctx.region).dp,
                              EV.request_for(code_spec.real_id, spans, level="session"))
    assert not denied.ok and "lambda:InvokeFunction" in (denied.error_message or ""), denied
    print("U-12c:", json.dumps({"invokedWith": "the caller's credentials",
                                "evidence": "a session with Evaluate only is denied lambda:InvokeFunction on the evaluator version",
                                "value": leak.value, "label": leak.label, "evaluatorId": code_spec.real_id}))
    print("evaluate:", json.dumps({k: [v.value, v.label] for k, v in outcomes.items()}))


def test_live_stop_session(world):
    """After `StopRuntimeSession` (the runner stops each session as soon as its last invocation returns), the session's
    spans are complete in `session_spans()` at the time they become ready, and no more arrive later."""
    from module11.aws import spans as SP
    from module11.release import trajectory
    run, record, spans, ready_at = run_one(world, SMALL)
    assert any(str(s.get("name")).startswith("invoke_agent") for s in spans)
    executed = trajectory.tool_sequence(spans)
    assert set(record.tools) <= set(executed), (record.tools, executed)                 # every completed tool has its span
    agent = [s for s in spans if str(s.get("name")).startswith("invoke_agent")]
    assert any((s.get("attributes") or {}).get("gen_ai.task.output") for s in agent), \
        "the invoke_agent span (the last to end) carries no output: its export was cut off"
    time.sleep(90)
    later = SP.session_spans(record.sessionId, world.target.log_group, record.startedAt, record.endedAt + 300,
                             logs=world.clients.logs)
    assert len(later) == len(spans), f"{len(later) - len(spans)} more records arrived after the session was ready"
    print("stop_session:", json.dumps({"records": len(spans), "tools": executed, "secondsToReady": round(ready_at - record.endedAt)}))
