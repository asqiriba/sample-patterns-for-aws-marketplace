# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The gate's concurrent control arm and relative latency rule (evaluation/control.py): the control
arm runs on prod `stable` in the same window and slots, is never evaluated or costed with the run, and the
p95 rule reuses the controller's `signals.latency`. The absolute p95 budget is an INFO line."""
from __future__ import annotations

import json

import numpy as np

from module11.evaluation import control as CT
from module11.evaluation import runner as R
from module11.release import gate as G
from module11.release import signals as S
from test_module11_runner import EVALUATORS, RUN, TARGET, Clock, World

HAPPY = [{"scenario_id": f"incident-triage-{i:03d}", "category": "happy_path", "team": "platform", "tags": [],
          "turns": [{"input": "Checkout has 502s."}], "assertions": [], "expected_response": "d-4471",
          "expected_trajectory": ["get_deployment_history"]} for i in range(1, 4)]
CONTROL = R.Target("prod", "stable", "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/m11_prod_companion-ABC",
                   "m11_prod_companion-ABC", "2026-10-08T00:08:26Z/4c0226e", "sha256:" + "b" * 64, "harness-v4", {})
STAGING = R.Target("staging", "staging", "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/m11_staging_companion-X",
                   "m11_staging_companion-X", "2026-10-08T01:20:28Z/29fc8e6", "sha256:" + "c" * 64, "harness-v5",
                   dict(TARGET.evaluator_ids))


def _run(world, clock, control_world=None, concurrency=4, **kw):
    specs = R.evaluator_specs(EVALUATORS, STAGING.evaluator_ids)
    arm = None
    if control_world is not None:
        arm = CT.ControlArm(R.Clients(core=control_world, logs=control_world, dp=control_world), CONTROL, HAPPY,
                            repeats=2, warmup=2, concurrency=concurrency)
    return R.SuiteRun(R.Clients(core=world, logs=world, dp=world), STAGING, HAPPY, repeats={"default": 3},
                      evaluators=specs, profile="staging", scope="full", evaluator_set_version="2",
                      suite="sha256:" + "9" * 64, run_id=RUN, concurrency=4, eval_concurrency=4, log=lambda line: None,
                      clock=clock, sleep=clock.sleep, control=arm, **kw).run()


class Gated(World):
    """Phase sessions wait on one barrier: it opens only when `parties` of them are in flight at once, which
    proves both arms run concurrently with their full pools (a shared pool of `concurrency` could never fill it).
    Tracks the most phase sessions in flight per arm."""

    def __init__(self, clock, barrier, inflight, arm):
        super().__init__(clock)
        self.barrier, self.inflight, self.arm = barrier, inflight, arm

    def invoke_agent_runtime(self, **kw):
        sid = kw["runtimeSessionId"]
        if not sid.startswith(("m11-control-", "m11-latency-")):
            return super().invoke_agent_runtime(**kw)
        with self.inflight["lock"]:
            self.inflight[self.arm] += 1
            self.inflight["max_" + self.arm] = max(self.inflight["max_" + self.arm], self.inflight[self.arm])
        try:
            if not self.inflight["opened"]:
                self.barrier.wait(timeout=10)
                self.inflight["opened"] = True
            return super().invoke_agent_runtime(**kw)
        finally:
            with self.inflight["lock"]:
                self.inflight[self.arm] -= 1


def test_both_arms_run_at_the_same_time_with_equal_pools():
    import threading
    clock = Clock()
    workers = 3
    inflight = {"lock": threading.Lock(), "candidate": 0, "control": 0, "max_candidate": 0, "max_control": 0,
                "opened": False}
    barrier = threading.Barrier(2 * workers)
    record = _run(Gated(clock, barrier, inflight, "candidate"), clock,
                  Gated(clock, barrier, inflight, "control"), concurrency=workers)
    assert not barrier.broken
    assert inflight["max_candidate"] == inflight["max_control"] == workers
    assert record.control["concurrency"] == {"candidate": workers, "control": workers}


def test_the_phase_is_tagged_and_excluded_from_every_other_statistic():
    clock = Clock()
    world, control_world = World(clock), World(clock)
    record = _run(world, clock, control_world)
    control = record.control
    assert control["design"] == "balanced" and control["sessions"] == 6 and control["candidateSessions"] == 6
    assert control["target"]["endpoint"] == "stable" and control["target"]["release"] == "harness-v4"
    # Control: 2 warm-up sessions, then 3 scenarios x 2 repeats, all on prod `stable`.
    assert len(control_world.invocations) == 8 and {q for _, q, _ in control_world.invocations} == {"stable"}
    phase = {"control": [(sid, env) for sid, _, env in control_world.invocations if sid.startswith("m11-control-")],
             "candidate": [(sid, env) for sid, _, env in world.invocations if sid.startswith("m11-latency-")]}
    assert len(phase["control"]) == len(phase["candidate"]) == 6
    for arm, prefix, req in (("control", "m11-control", "ct-"), ("candidate", "m11-latency", "lt-")):
        for sid, env in phase[arm]:
            assert sid.startswith(f"{prefix}-{RUN}-") and env["request"]["request_id"].startswith(req)
            assert env["context"]["variant"] == "eval" and env["context"]["shadow"] is True
    assert {env["context"]["manifestVersion"] for _, env in phase["control"]} == {CONTROL.manifest_version}
    assert {env["context"]["manifestVersion"] for _, env in phase["candidate"]} == {STAGING.manifest_version}
    assert control_world.evaluations == []                              # never evaluated
    assert all("m11-latency-" not in json.dumps(kw) for kw in world.evaluations)
    # The main suite's record, statistics and cost are its own sessions only.
    assert len(record.sessions) == 9 and all(s["sessionId"].startswith(f"m11-eval-{RUN}-") for s in record.sessions)
    assert all(r["sessionId"].startswith("m11-eval-") for r in record.results)
    assert record.cost["sessions"] == 9 and control["costUsd"] >= 0
    assert "latency_phase_s" in record.timings
    rows = {s["sessionId"] for s in control["candidateRecords"]} | {s["sessionId"] for s in control["records"]}
    assert all(sid.startswith(("m11-latency-", "m11-control-")) for sid in rows)


def test_without_a_control_arm_the_record_has_none():
    clock = Clock()
    record = _run(World(clock), clock)
    assert record.control == {}


# ---------------------------------------------------------------- transport re-invokes
class PreDispatch(World):
    """The first invoke of each scenario in `fail` raises `code` before the container runs; later ones work.
    `spans_for` holds session IDs whose span/log check finds records (the request reached the container)."""

    def __init__(self, clock, fail, code="RuntimeClientError", spans_for=(), fail_again=False):
        super().__init__(clock)
        self.fail, self.code, self.fail_again = dict.fromkeys(fail, 0), code, fail_again
        self.spans_for, self.filtered = set(spans_for), []

    def invoke_agent_runtime(self, **kw):
        from botocore.exceptions import ClientError
        scenario = json.loads(kw["payload"])["context"]["scenarioId"]
        if scenario in self.fail and (self.fail[scenario] == 0 or self.fail_again):
            self.fail[scenario] += 1
            with self.lock:
                self.clock.t += 5
            raise ClientError({"Error": {"Code": self.code}}, "InvokeAgentRuntime")
        return super().invoke_agent_runtime(**kw)

    def filter_log_events(self, **kw):
        term = kw.get("filterPattern", "").strip('"')
        self.filtered.append(term)
        if (term in self.spans_for or any(term.startswith(p) for p in self.spans_for)) \
                and kw.get("logGroupName") != "aws/spans":
            return {"events": [{"message": json.dumps({
                "scope": {"name": "opentelemetry.instrumentation.devops_companion"}, "name": "invoke_agent",
                "traceId": "0" * 32, "startTimeUnixNano": "1000", "attributes": {"session.id": term}})}]}
        if any(term == sid for sid in self.sessions):
            return super().filter_log_events(**kw)
        return {"events": []}


def _transport_run(world, clock):
    specs = R.evaluator_specs(EVALUATORS, STAGING.evaluator_ids)
    return R.SuiteRun(R.Clients(core=world, logs=world, dp=world), STAGING, HAPPY, repeats={"default": 1},
                      evaluators=specs, profile="staging", scope="full", evaluator_set_version="2",
                      suite="sha256:" + "9" * 64, run_id=RUN, concurrency=2, eval_concurrency=2, log=lambda line: None,
                      clock=clock, sleep=clock.sleep).run()


def test_a_pre_dispatch_failure_is_re_invoked_once_with_a_new_session():
    clock = Clock()
    world = PreDispatch(clock, ["incident-triage-002"])
    record = _transport_run(world, clock)
    (entry,) = record.transport["reinvokes"]
    assert entry["reinvoked"] and entry["error"] == "RuntimeClientError"
    assert entry["evidence"] == "no span document and no runtime log line"
    assert entry["sessionId"] != entry["originalSessionId"] and entry["status"] == "completed"
    assert record.transport["tripped"] is None
    retried = next(s for s in record.sessions if s["scenarioId"] == "incident-triage-002")
    assert retried["sessionId"] == entry["sessionId"] and retried["reinvokeOf"] == entry["originalSessionId"]
    assert all(r["sessionId"] != entry["originalSessionId"] for r in record.results)
    assert {r["evaluatorId"] for r in record.results if r["sessionId"] == entry["sessionId"]}  # evaluated
    sent = [env for sid, _, env in world.invocations if sid == entry["sessionId"]]
    assert sent and sent[0]["request"]["change_summary"] == HAPPY[1]["turns"][0]["input"]   # same input


def test_no_re_invoke_when_the_session_left_spans_or_logs():
    clock = Clock()
    world = PreDispatch(clock, ["incident-triage-002"], spans_for=["m11-eval-"])
    record = _transport_run(world, clock)
    (entry,) = record.transport["reinvokes"]
    assert not entry["reinvoked"] and "span documents or runtime log lines" in entry["evidence"]
    failed = next(s for s in record.sessions if s["scenarioId"] == "incident-triage-002")
    assert failed["status"] == "invoke_error" and failed["sessionId"] == entry["originalSessionId"]


def test_no_re_invoke_for_an_error_that_is_not_pre_dispatch():
    clock = Clock()
    record = _transport_run(PreDispatch(clock, ["incident-triage-002"], code="ResponseStreamingError"), clock)
    assert record.transport["reinvokes"] == [] and record.transport["tripped"] is None


def test_the_circuit_breaker_trips_on_too_many_or_a_failed_re_invoke():
    clock = Clock()
    record = _transport_run(PreDispatch(clock, [s["scenario_id"] for s in HAPPY]), clock)
    assert record.transport["tripped"] == "3 pre-dispatch failures (limit 2)"
    assert record.transport["reinvokes"] == [] and len(record.transport["skipped"]) == 3
    clock = Clock()
    record = _transport_run(PreDispatch(clock, ["incident-triage-002"], fail_again=True), clock)
    assert record.transport["tripped"].startswith("the re-invoke of m11-eval-") and "also failed" in \
        record.transport["tripped"]


def test_the_report_counts_transport_re_invokes():
    from module11.evaluation import provider_health as PH
    text = PH.transport_markdown({"reinvokes": [
        {"scenarioId": "incident-triage-002", "repeat": 1, "originalSessionId": "m11-eval-a", "error":
         "RuntimeClientError", "evidence": "no span document and no runtime log line", "reinvoked": True,
         "sessionId": "m11-eval-b", "status": "completed"}], "tripped": None})
    assert text == ("**Transport re-invokes:** 1: incident-triage-002#1 m11-eval-a (RuntimeClientError; no span "
                    "document and no runtime log line) -> m11-eval-b completed.")
    tripped = PH.transport_markdown({"reinvokes": [], "tripped": "3 pre-dispatch failures (limit 2)",
                                     "skipped": [{"sessionId": "x", "error": "RuntimeClientError"}]})
    assert "Circuit breaker tripped: 3 pre-dispatch failures (limit 2)" in tripped
    assert PH.transport_markdown(None) == "**Transport re-invokes:** 0."
    doc = PH.collect(None, ["m"], [], transport={"reinvokes": [], "tripped": "x"})
    assert "Circuit breaker tripped: x" in PH.markdown(doc)


def test_the_traffic_driver_counts_but_never_re_sends_invocation_failures():
    from module11.demo import traffic as T
    ok = T.Session("payments", "r1", "s", httpStatus=200, status="completed")
    bad = T.Session("payments", "r2", "s", httpStatus=502, status="http-502")
    assert not T.is_invocation_failed(ok) and T.is_invocation_failed(bad)
    import inspect
    assert "invocationFailed" in inspect.getsource(T.Driver.summary) and '"reSent": 0' in inspect.getsource(T.Driver.summary)


def test_session_policy_allows_only_invoke_and_stop():
    doc = json.loads(CT.session_policy(CONTROL.runtime_arn))
    (statement,) = doc["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"]
    assert statement["Resource"] == [CONTROL.runtime_arn, CONTROL.runtime_arn + "/*"]


# ---------------------------------------------------------------- the rule
def test_judge_is_the_controllers_latency_signal():
    rng = np.random.default_rng(3)
    same = list(rng.lognormal(9.5, 0.3, 60)), list(rng.lognormal(9.5, 0.3, 40))
    assert CT.judge(*same)["status"] == "PASS"
    slow = [x * 2 for x in same[0]]
    out = CT.judge(slow, same[1])
    assert out["status"] == "FAIL" and out["n"] == 60 and out["controlN"] == 40 and "above p=" in out["detail"]
    marker = {"cache_n": 1, "cache_sum": 1.0}
    expected = S.latency(S.ArmAgg(n=60, latencies=slow, **marker), S.ArmAgg(n=40, latencies=same[1], **marker),
                         CT.ALPHA, CT.MIN_SESSIONS)
    assert expected.status == S.BREACH and expected.detail == out["detail"]
    assert CT.judge(slow[:10], same[1])["status"] == "INCONCLUSIVE"
    assert CT.ALPHA == 0.01 and CT.CONTROL_REPEATS == 2 and CT.MIN_SESSIONS == 20


def test_a_slow_provider_slows_both_arms_and_passes():
    """2026-10-07's failure mode: the provider's tail hits both arms in the same window."""
    rng = np.random.default_rng(11)
    tail = lambda n: [x * (3.0 if rng.random() < 0.08 else 1.0) for x in rng.lognormal(9.5, 0.3, n)]
    candidate, control = tail(60), tail(40)
    assert CT.p95(candidate) > 1.5 * float(np.exp(9.5 + 1.645 * 0.3))      # well past an untailed p95
    assert CT.judge(candidate, control)["status"] == "PASS"


def test_latencies_count_the_sessions_that_reached_the_runtime():
    rows = [{"category": "happy_path", "status": "completed", "latencyMs": 1.0},
            {"category": "happy_path", "status": "failed", "latencyMs": 2.0},
            {"category": "happy_path", "status": "invoke_error", "latencyMs": 3.0},
            {"category": "edge_cases", "status": "completed", "latencyMs": 4.0}]
    assert CT.latencies(rows) == [1.0, 2.0]
    assert CT.scenarios_for([{"category": "happy_path"}, {"category": "regressions"}]) == [{"category": "happy_path"}]


# ---------------------------------------------------------------- gate.evaluate
THRESHOLDS = {"gates": {}, "budgets": {"p95LatencyMs": 41000, "relativeToBaseline": 0.25}}
SCEN = [{"scenario_id": "incident-triage-001", "category": "happy_path", "tags": []}]


def _metrics(latency):
    return [G.SessionMetrics(f"s{i}", latency, 100, 10, 5, "completed") for i in range(3)]


def _evaluate(latency, control):
    return G.evaluate(THRESHOLDS, SCEN, [], _metrics(latency), None, None, scope="full",
                      profile={"repeats": {"default": 1}}, evaluator_set_version="2", suite="x", evaluators=[],
                      latency_control=control)


def test_the_absolute_budget_becomes_an_info_line_and_the_control_line_gates():
    report = _evaluate(50000.0, {"status": "PASS", "p95Ms": 50000.0, "controlP95Ms": 48000.0, "n": 60,
                                 "controlN": 40, "alpha": 0.01, "minSessions": 20, "effect": 1.25, "detail": ""})
    ux = next(line for line in report.lines if line.name == G.UX_BUDGET)
    assert ux.status == G.INFO and ux.detail.startswith("over; informational, not gated")
    ctl = next(line for line in report.lines if line.name == G.CONTROL_LINE)
    assert ctl.status == G.PASS and ctl.observed == "50000 vs control 48000 (60 vs 40 sessions)"
    assert report.verdict == G.PASS                     # over the absolute budget, yet not slower than control
    assert "| p95LatencyMs (user-experience budget) | full | INFO |" in report.markdownTable()
    fail = _evaluate(20000.0, {"status": "FAIL", "p95Ms": 20000.0, "controlP95Ms": 9000.0, "n": 60, "controlN": 40,
                               "detail": "p95 20000 vs 9000, 12/60 above p=0.0001"})
    assert fail.verdict == G.FAIL
    assert _evaluate(20000.0, {"status": "INCONCLUSIVE", "n": 5, "controlN": 40}).verdict == G.INCONCLUSIVE
    skip = _evaluate(20000.0, {"status": "SKIP", "reason": "no happy_path scenarios in scope"})
    assert next(line for line in skip.lines if line.name == G.CONTROL_LINE).status == G.SKIP


def test_without_control_the_p95_budget_gates_as_before():
    report = _evaluate(50000.0, None)
    line = next(line for line in report.lines if line.name == "p95LatencyMs")
    assert line.status == G.FAIL and report.verdict == G.FAIL
    assert not any(line.name == G.CONTROL_LINE for line in report.lines)


def test_the_report_carries_the_control_block():
    from module11.cli_eval import _report_dict
    report = G.GateReport(verdict="PASS", scope="full", comparable=True, reasons=[], lines=[])
    block = {"design": "balanced", "target": {"release": "harness-v4", "endpoint": "stable", "manifestVersion": "mv"},
             "sessions": 40, "candidateSessions": 40, "concurrency": {"candidate": 12, "control": 12},
             "warmup": {"sessions": 6}, "seconds": 61.0, "costUsd": 4.04, "records": [{"x": 1}]}
    stored = _report_dict(report, {"sessions": 0}, [], None, control=block)
    assert stored["control"].startswith("**Latency control phase:** 40 candidate and 40 control happy_path session(s) "
                                        "at the same time, 12 workers per arm; control = harness-v4 on prod `stable`")
    assert "$4.04" in stored["control"] and stored["control"] in stored["markdown"]
    assert _report_dict(report, {"sessions": 0}, [], None)["control"] is None
    assert "not run (no happy_path scenarios in scope)" in CT.markdown({"reason": "no happy_path scenarios in scope"})


def test_the_rule_compares_the_balanced_arms_not_the_main_suite():
    from module11.cli_eval import latency_control
    from module11.evaluation.records import RunRecord
    slow_main = [{"category": "happy_path", "status": "completed", "latencyMs": 90000.0}] * 30
    arm = [{"category": "happy_path", "status": "completed", "latencyMs": 10000.0 + i} for i in range(40)]
    record = RunRecord("r", "gate", "staging", "staging", "staging", "full", {}, "mv", "bd", "rel", "2", "s", [],
                       sessions=slow_main, control={"design": "balanced", "records": arm, "candidateRecords": arm})
    out = latency_control(record, [{"category": "happy_path"}])
    assert out["status"] == "PASS" and out["n"] == 40 and out["controlN"] == 40


def test_each_arm_gets_forty_sessions_in_every_scope():
    """The MR gate's tag:fast scope has 4 happy_path scenarios: 2 repeats gave 8 vs 8 (< MIN_SESSIONS, always
    INCONCLUSIVE); repeats_for sizes both arms to ARM_SESSIONS. A full scope (20 scenarios) keeps 2 repeats."""
    def scen(n):
        return [{"scenario_id": f"h{i}", "category": "happy_path"} for i in range(n)]
    assert CT.repeats_for(scen(20)) == CT.CONTROL_REPEATS == 2
    assert CT.repeats_for(scen(4)) == 10 and 4 * CT.repeats_for(scen(4)) >= CT.MIN_SESSIONS
    assert CT.repeats_for(scen(3)) * 3 >= CT.ARM_SESSIONS == 40
    assert CT.repeats_for([]) == CT.CONTROL_REPEATS
