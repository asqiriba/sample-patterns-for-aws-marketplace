# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Gate warm-up (evaluation/warmup.py): warm-up sessions are excluded
from every statistic, the report carries the informational warm-up block, and warm-up sessions cause no side
effect (shadow envelope; the local runtime writes no note and no Memory record)."""
from __future__ import annotations

import json

import pytest

from module11.evaluation import runner as R
from module11.evaluation import warmup as WU
from module11.release import gate as G
from module11.runtime import envelope as E
from test_module11_runner import RUN, SCENARIOS, TARGET, Clock, World, make_run
from test_module11_support import invoke, local_dir, make_runtime


class Recording(World):
    """World that also records which methods each session ID saw."""

    def __init__(self, clock):
        super().__init__(clock)
        self.calls = []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(("invoke", kw["runtimeSessionId"]))
        return super().invoke_agent_runtime(**kw)

    def stop_runtime_session(self, **kw):
        self.calls.append(("stop", kw["runtimeSessionId"]))
        return super().stop_runtime_session(**kw)

    def filter_log_events(self, logGroupName, filterPattern, **kw):
        self.calls.append(("spans", filterPattern.strip('"')))
        return super().filter_log_events(logGroupName, filterPattern, **kw)

    def evaluate(self, **kw):
        self.calls.append(("evaluate", json.dumps(kw)))
        return super().evaluate(**kw)


def test_warmup_sessions_are_excluded_from_every_statistic():
    clock = Clock()
    cold, warm = World(clock), Recording(clock)
    without = make_run(cold, clock).run()
    record = make_run(warm, clock, warmup=3).run()
    warm_ids = record.warmup["sessionIds"]
    assert record.warmup["sessions"] == 3 and len(warm_ids) == 3
    assert all(sid.startswith(f"m11-warmup-{RUN}-") and 33 <= len(sid) <= 256 for sid in warm_ids)
    # Same sessions, results, metrics, coverage and cost as a run without warm-up.
    assert len(record.sessions) == len(without.sessions) and len(record.results) == len(without.results)
    assert not {s["sessionId"] for s in record.sessions} & set(warm_ids)
    assert not {r["sessionId"] for r in record.results} & set(warm_ids)
    assert not {m.sessionId for m in record.metrics()} & set(warm_ids)
    assert record.cost["sessions"] == without.cost["sessions"]
    # Never fetched or evaluated: span reads and Evaluate calls name only the suite's sessions.
    assert not [c for c in warm.calls if c[0] == "spans" and c[1] in warm_ids]
    assert not [c for c in warm.calls if c[0] == "evaluate" and any(w in c[1] for w in warm_ids)]
    # Warm-up comes first and each warm-up session is stopped.
    first_eval = next(i for i, c in enumerate(warm.calls) if c[0] == "invoke" and c[1].startswith("m11-eval-"))
    assert {c[1] for c in warm.calls[:first_eval] if c[0] == "invoke"} == set(warm_ids)
    assert {c[1] for c in warm.calls if c[0] == "stop"} >= set(warm_ids)


def test_warmup_does_not_change_the_gate_report():
    def report_of(warmup):
        clock = Clock()
        record = make_run(World(clock), clock, warmup=warmup).run()
        thresholds = {"gates": {"hard": [], "statistical": []}, "budgets": {}}
        report = G.evaluate(thresholds, SCENARIOS, record.eval_results(), record.metrics(), None, None,
                            scope="tag:fast", profile={"repeats": {"default": 2, "adversarial": 1}},
                            evaluator_set_version="2", suite=record.suiteSha256, evaluators=list(record.evaluators))
        return [(l.name, l.status, l.observed) for l in report.lines]
    lines = report_of(4)
    assert lines == report_of(0) and lines[0][0] == "coverage" and lines[0][1] == "PASS"


def test_report_carries_the_informational_block():
    from module11.cli_eval import _report_dict
    block = WU.summarize([{"sessionId": "a", "status": "completed", "timeToFirstEventMs": 4000.0, "latencyMs": 30000.0},
                          {"sessionId": "b", "status": "completed", "timeToFirstEventMs": 9000.0, "latencyMs": 40000.0},
                          {"sessionId": "c", "status": "invoke_error", "timeToFirstEventMs": None, "latencyMs": None}])
    assert block["timeToFirstEventMedianMs"] == 6500.0 and block["timeToFirstEventMaxMs"] == 9000.0
    text = WU.markdown(block)
    assert "Warm-up (informational, not scored)" in text and "3 untimed shadow session(s)" in text
    assert "median 6.5 s, max 9.0 s" in text and "1 invoke error(s)" in text
    report = G.GateReport(verdict="PASS", scope="full", comparable=True, reasons=[], lines=[])
    stored = _report_dict(report, block)
    assert stored["warmup"] == text and text in stored["markdown"]
    assert "none (cold start not measured)" in WU.markdown({"sessions": 0})


def test_report_counts_guard_blocked_attempts():
    from module11.cli_eval import _report_dict, guard_blocked_markdown
    sessions = [{"scenarioId": "regression-runbook-skip-001", "repeat": 4, "guardBlocked": 1},
                {"scenarioId": "incident-triage-014", "repeat": 1, "guardBlocked": 0}]
    text = guard_blocked_markdown(sessions)
    assert "Guard-blocked attempts (informational, not scored):** 1 in 1 session(s)" in text
    assert "regression-runbook-skip-001#4 (1)" in text
    assert guard_blocked_markdown([]).endswith(" 0.")
    report = G.GateReport(verdict="PASS", scope="full", comparable=True, reasons=[], lines=[])
    stored = _report_dict(report, {"sessions": 0}, sessions)
    assert stored["guardBlocked"] == text and text in stored["markdown"]


def test_runner_records_guard_blocked_events():
    class Blocking(World):
        def invoke_agent_runtime(self, **kw):
            out = super().invoke_agent_runtime(**kw)
            body = out["response"].getvalue().decode()
            body = body.replace('"events": [', '"events": [{"agent": "DevOps Companion", "tool": '
                                '"delegate_recovery_planning", "status": "failed", "error": "guard_blocked"}, ', 1)
            out["response"] = __import__("io").BytesIO(body.encode())
            return out
    clock = Clock()
    record = make_run(Blocking(clock), clock).run()
    completed = [s for s in record.sessions if s["status"] == "completed"]
    assert completed and all(s["guardBlocked"] == s["turns"] for s in completed)   # one injected event per turn
    assert all("delegate_recovery_planning" not in s["tools"] for s in completed)   # never counted as completed


def test_warmup_size_is_configurable(monkeypatch):
    assert WU.sessions_from(None) == 12
    monkeypatch.setenv(WU.ENV, "6")
    assert WU.sessions_from(None) == 6 and WU.sessions_from(0) == 0 and WU.sessions_from("3") == 3
    with pytest.raises(ValueError):
        WU.sessions_from(64)
    from module11.cli import parser
    assert parser().parse_args(["gate", "--stage", "staging", "--warmup", "4"]).warmup == 4
    assert parser().parse_args(["pipeline", "gate", "--warmup", "0"]).warmup == 0


def test_warmup_envelope_is_shadow_tagged_and_stamped():
    env = WU.envelope(TARGET, RUN, 7)
    inv = E.parse(env)
    assert inv.shadow is True and inv.variant == "eval" and inv.scenario_id == WU.SCENARIO_TAG
    assert inv.manifest_version == TARGET.manifest_version and inv.release == TARGET.release
    assert inv.request_id.startswith(f"wu-{RUN}-")


def test_warmup_session_has_no_side_effects(tmp_path):
    d = local_dir(tmp_path)
    runtime = make_runtime(d)
    context = json.loads((d / "envelope-investigation.json").read_text())["context"]
    target = R.Target("local", "local", "arn:local", "local", context["manifestVersion"], context["behaviorDigest"],
                      context["release"])
    body, _ = invoke(runtime, WU.envelope(target, RUN, 1))
    assert body["status"] == "completed" and body["m11"]["variant"] == "eval"
    side_effects = d / "bucket" / "side-effects"
    assert not side_effects.is_dir() or not list(side_effects.rglob("*.json"))
    memory = d / "memory" / "records.jsonl"
    assert not memory.is_file() or memory.read_text().strip() == ""
