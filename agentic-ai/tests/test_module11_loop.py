# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The parameterized agent loop with a fake model client (8.1 test_module11_loop):
advertised-only dispatch, required tools and limits from orchestration.yaml, schema-validated
arguments, the delegation input and guard, guardrail INPUT/OUTPUT, and context.data on every call."""
import itertools
import json

import pytest

from module11.release import manifest as M
from module11.runtime import fake_model as F
from module11.runtime import guardrail as G
from module11.runtime.config import BundleSource
from module11.runtime.tools import LocalInvoker
from test_module11_support import ROOT, config_doc, envelope, invoke, local_dir, make_runtime

SVC = {"service_name": "checkout-service"}
_ids = itertools.count()


@pytest.fixture
def d(tmp_path):
    return local_dir(tmp_path)


def calls(*items):
    """One model turn that requests the given (tool, arguments) calls."""
    return F.FakeResponse([F.call(name, args, next(_ids)) for name, args in items])


class Script:
    """Companion turns from a queue; the planner (offered get_blast_radius) uses the default policy."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests = []

    def __call__(self, params):
        self.requests.append(params)
        if "get_blast_radius" in {t["name"] for t in params.get("tools") or []}:
            return F.FakeModelClient.default(params)
        return self.turns.pop(0) if self.turns else F.text("Done. Proposal, not executed: roll back.")


class RecordingInvoker:
    def __init__(self):
        self.events = []
        self.local = LocalInvoker()

    def invoke(self, arn, event, timeout):
        self.events.append((arn, json.loads(json.dumps(event))))
        return self.local.invoke(arn, event, timeout)


class RecordingGuardrail:
    def __init__(self, block=None, fail=False):
        self.calls = []
        self.block, self.fail = block, fail

    def apply(self, text, source):
        self.calls.append((source, text))
        if self.fail:
            raise G.GuardrailUnavailable("EndpointConnectionError")
        if source == self.block:
            return G.GuardrailResult(G.INTERVENED, "Blocked by the demo guardrail.")
        return G.GuardrailResult("NONE", text)


def run(d, script, *, planning=True, change_summary=None, invoker=None, guardrail=None, bundles=None):
    doc = envelope(d)
    if change_summary:
        doc["request"]["change_summary"] = change_summary
    runtime = make_runtime(d, model=F.FakeModelClient(script), planning=planning, invoker=invoker,
                           guardrail=guardrail, bundles=bundles)
    return invoke(runtime, doc)


def edited_bundles(edit):
    component = M.bundle_component(ROOT)
    edit(component)
    return BundleSource(lambda arn, version: {"components": {"local:runtime": {"configuration": component}}})


# ---------------------------------------------------------------- dispatch and required tools
def test_default_fake_run_follows_the_prompt_order(d):
    invoker = RecordingInvoker()
    body, events = run(d, F.FakeModelClient.default, invoker=invoker)
    assert body["status"] == "completed"
    tools = [e["tool"] for e in body["events"]]
    assert tools[0] == "get_deployment_history"
    assert set(tools) == {"get_deployment_history", "query_metrics", "search_runbooks", "get_service_logs",
                          "record_incident_note"}
    assert [name for name, data in events if name == "tool"] == ["tool"] * len(tools)
    assert "d-4471" in body["review"] and "Proposal, not executed:" in body["review"]


def test_only_advertised_tools_dispatch(d):
    body, _ = run(d, Script(calls(("get_deployment_history", SVC)), calls(("get_blast_radius", SVC))))
    assert body["status"] == "failed" and "unavailable capability: get_blast_radius" in body["detail"]
    assert body["events"][-1] == {"agent": "DevOps Companion", "tool": "get_blast_radius", "status": "failed"}


def test_delegate_is_not_offered_or_dispatched_when_the_flag_is_off(d):
    script = Script(calls(("get_deployment_history", SVC)), calls(("delegate_recovery_planning", SVC)))
    body, _ = run(d, script, planning=False)
    offered = {t["name"] for t in script.requests[0]["tools"]}
    assert "delegate_recovery_planning" not in offered and "record_incident_note" in offered
    assert body["status"] == "failed" and "unavailable capability: delegate_recovery_planning" in body["detail"]
    assert body["m11"]["toolsOffered"] == sorted(offered)


def test_skipping_search_runbooks_completes(d):
    script = Script(calls(("get_deployment_history", SVC)), calls(("query_metrics", SVC), ("get_service_logs", SVC)))
    body, _ = run(d, script)
    assert body["status"] == "completed"
    assert "search_runbooks" not in [e["tool"] for e in body["events"]]


def test_skipping_get_deployment_history_raises_agent_run_error(d):
    body, _ = run(d, Script(calls(("query_metrics", SVC)), F.text("It is the deploy.")))
    assert body["status"] == "failed"
    assert body["detail"] == "DevOps Companion finished without the required evidence checks"


# ---------------------------------------------------------------- an incomplete session still answers
def test_missing_required_evidence_ends_with_a_plain_answer_naming_what_is_missing(d):
    """The model's text is kept, and the answer says the required check is missing; the session stays failed."""
    body, events = run(d, Script(calls(("query_metrics", SVC)), F.text("It is the deploy.")))
    assert body["status"] == "failed" and body["detail"].endswith("without the required evidence checks")
    review = body["review"]
    assert review.startswith("It is the deploy.")
    assert "This investigation is incomplete" in review and "Evidence checks completed: query_metrics." in review
    assert "Required evidence missing: get_deployment_history (not run)." in review
    assert "no change was made" in review
    assert [data["tool"] for name, data in events if name == "tool"] == ["query_metrics"]


def test_a_failed_required_tool_is_named_with_its_error_code(d):
    """The probe case: the required tool ran but failed (a name that is no known service -> not_found;
    a unique short name such as "checkout" now resolves, aws/evidence.resolve_service)."""
    script = Script(calls(("get_deployment_history", {"service_name": "cart"})), F.text("No evidence."))
    body, _ = run(d, script)
    assert body["status"] == "failed"
    assert "get_deployment_history (failed: " in body["review"]


def test_a_planner_refusal_ends_with_a_plain_answer(d):
    """delegate_recovery_planning before get_deployment_history, then the model stops without the history: the
    guard's refusal was recoverable, but the session still ends with a plain answer naming the missing evidence."""
    body, _ = run(d, Script(calls(("delegate_recovery_planning", SVC))))
    assert body["status"] == "failed" and body["detail"] == "DevOps Companion finished without the required evidence checks"
    assert body["review"].startswith("Done. Proposal, not executed: roll back.\n\nThis investigation is incomplete: "
                                     "DevOps Companion finished without the required evidence checks.")
    assert "Evidence checks completed: none." in body["review"]
    assert "Required evidence missing: get_deployment_history (not run)." in body["review"]
    assert _companion_tools(body) == [("delegate_recovery_planning", "failed", "guard_blocked")]


def test_the_incomplete_answer_passes_the_output_guardrail(d):
    guard = RecordingGuardrail(block="OUTPUT")
    body, _ = run(d, Script(calls(("query_metrics", SVC)), F.text("It is the deploy.")), guardrail=guard)
    assert body["status"] == "failed" and "review" not in body
    assert guard.calls[-1][0] == "OUTPUT" and "This investigation is incomplete" in guard.calls[-1][1]


def test_a_complete_session_is_unchanged(d):
    body, _ = run(d, F.FakeModelClient.default)
    assert body["status"] == "completed" and "This investigation is incomplete" not in body["review"]


# ---------------------------------------------------------------- limits from orchestration
def test_turn_limit_comes_from_orchestration(d):
    bundles = edited_bundles(lambda c: c["orchestration"].update(maxTurns=2))
    script = Script(*[calls(("get_deployment_history", SVC))] + [calls(("query_metrics", SVC))] * 5)
    body, _ = run(d, script, bundles=bundles)
    assert body["detail"] == "DevOps Companion exceeded its model-turn limit"
    assert len(script.requests) == 2


def test_tool_call_limit_comes_from_orchestration(d):
    bundles = edited_bundles(lambda c: c["orchestration"].update(maxToolCalls=2))
    script = Script(calls(("get_deployment_history", SVC), ("query_metrics", SVC), ("get_service_logs", SVC)))
    body, _ = run(d, script, bundles=bundles)
    assert body["detail"] == "DevOps Companion exceeded the tool-call limit"


@pytest.mark.parametrize("deadline,expected", [(5, "The investigation exceeded its time budget"), (150, None)])
def test_deadline_comes_from_orchestration(d, monkeypatch, deadline, expected):
    import time
    ticks = itertools.count()
    monkeypatch.setattr(time, "monotonic", lambda: 3.0 * next(ticks))   # every clock read is 3 s later
    bundles = edited_bundles(lambda c: c["orchestration"].update(deadlineSeconds=deadline))
    body, _ = run(d, Script(calls(("get_deployment_history", SVC))), bundles=bundles)
    assert body.get("detail") == expected


def test_model_parameters_come_from_models_yaml(d):
    script = Script(calls(("get_deployment_history", SVC)))
    run(d, script, bundles=edited_bundles(lambda c: c["models"]["companion"].update(maxOutputTokens=321,
                                                                                    reasoningEffort="low")))
    request = script.requests[0]
    assert (request["max_output_tokens"], request["reasoning"], request["store"]) == (321, {"effort": "low"}, False)
    assert request["model"] == M.bundle_component(ROOT)["models"]["companion"]["modelId"]
    assert all(tool["strict"] is True and "$schema" not in tool["parameters"] for tool in request["tools"])


def test_mutating_tool_runs_at_most_once(d):
    note = {**SVC, "probable_cause": "Pool too small.", "log_line": "upstream connect error"}
    script = Script(calls(("get_deployment_history", SVC)), calls(("record_incident_note", note)),
                    calls(("record_incident_note", note)))
    body, _ = run(d, script)
    assert body["detail"] == "DevOps Companion attempted to repeat record_incident_note"


# ---------------------------------------------------------------- arguments
@pytest.mark.parametrize("arguments", [{}, {"service_name": "Checkout Service"}, {**SVC, "extra": 1}])
def test_schema_invalid_arguments_become_a_tool_error_and_the_loop_continues(d, arguments):
    invoker = RecordingInvoker()
    script = Script(calls(("query_metrics", arguments)), calls(("get_deployment_history", SVC)))
    body, _ = run(d, script, invoker=invoker)
    assert body["status"] == "completed"
    first = body["events"][0]
    assert (first["tool"], first["status"], first["error"]) == ("query_metrics", "failed", "invalid_arguments")
    assert [arn for arn, _ in invoker.events] == ["local:get_deployment_history"]   # no Lambda call for bad input
    output = next(i for i in script.requests[1]["input"] if i.get("type") == "function_call_output")
    assert json.loads(output["output"])["error"]["code"] == "invalid_arguments"


def test_non_json_arguments_are_an_invalid_arguments_error(d):
    bad = F.FakeResponse([F.FakeItem("function_call", name="query_metrics", arguments="{not json", call_id="c-bad")])
    body, _ = run(d, Script(bad, calls(("get_deployment_history", SVC))))
    assert body["events"][0]["error"] == "invalid_arguments" and body["status"] == "completed"


# ---------------------------------------------------------------- delegation
def test_delegation_input_and_draft(d):
    script = Script(calls(("get_deployment_history", SVC)), calls(("query_metrics", SVC)),
                    calls(("delegate_recovery_planning", SVC)))
    body, _ = run(d, script, change_summary="Checkout 502s after the deploy. Plan the recovery.")
    assert body["status"] == "completed"
    planner_first = next(r for r in script.requests if "get_blast_radius" in {t["name"] for t in r["tools"]})
    task = json.loads(planner_first["input"][0]["content"])
    assert set(task) == {"request", "deployment_evidence", "metrics"}
    assert task["request"] == "Checkout 502s after the deploy. Plan the recovery."
    assert task["deployment_evidence"]["source"] and task["metrics"]["service"] == "checkout-service"
    assert {t["name"] for t in planner_first["tools"]} == {"get_blast_radius", "check_rollback_readiness"}
    assert body["recovery_plan"]["requires_human_review"] is True and body["recovery_plan"]["changes_executed"] is False
    agents = [e["agent"] for e in body["events"]]
    assert "Recovery Planner" in agents and agents.count("DevOps Companion") >= 3


PLAN = "Plan the checkout recovery."


def _companion_tools(body):
    return [(e["tool"], e["status"], e.get("error")) for e in body["events"] if e["agent"] == "DevOps Companion"]


def test_out_of_order_delegation_is_blocked_then_recovered(d):
    """The guard refuses a delegation before deployment history with a recoverable tool error; the planner does not
    run; the model gathers the history, delegates again, and the session completes."""
    script = Script(calls(("delegate_recovery_planning", SVC)), calls(("get_deployment_history", SVC)),
                    calls(("delegate_recovery_planning", SVC)))
    body, _ = run(d, script, change_summary=PLAN)
    assert body["status"] == "completed"
    tools = _companion_tools(body)
    assert tools[0] == ("delegate_recovery_planning", "failed", "guard_blocked")
    assert tools[1] == ("get_deployment_history", "completed", None)
    assert tools[2] == ("delegate_recovery_planning", "completed", None)
    planner = [i for i, e in enumerate(body["events"]) if e["agent"] == "Recovery Planner"]
    history = next(i for i, e in enumerate(body["events"]) if e["tool"] == "get_deployment_history")
    assert planner and min(planner) > history                       # delegation never executes before history
    # The model saw the refusal as a tool result it could act on.
    second = script.requests[1]["input"]
    refusal = [m for m in second if isinstance(m, dict) and m.get("type") == "function_call_output"]
    assert refusal and "guard_blocked" in refusal[0]["output"] and "get_deployment_history" in refusal[0]["output"]


def test_repeated_out_of_order_delegation_ends_with_a_plain_answer(d):
    script = Script(calls(("delegate_recovery_planning", SVC)), calls(("delegate_recovery_planning", SVC)))
    body, _ = run(d, script, change_summary=PLAN)
    assert body["status"] == "failed"
    assert body["detail"] == "DevOps Companion requested recovery planning before deployment history 2 times"
    assert "This investigation is incomplete" in body["review"]
    assert "Required evidence missing: get_deployment_history (not run)." in body["review"]
    assert not any(e["agent"] == "Recovery Planner" for e in body["events"])
    assert [t[2] for t in _companion_tools(body)] == ["guard_blocked", "guard_blocked"]


def test_in_order_delegation_is_unchanged(d):
    script = Script(calls(("get_deployment_history", SVC)), calls(("delegate_recovery_planning", SVC)))
    body, _ = run(d, script, change_summary=PLAN)
    assert body["status"] == "completed"
    assert [t[1] for t in _companion_tools(body)] == ["completed", "completed"]
    assert any(e["agent"] == "Recovery Planner" for e in body["events"])


def test_a_blocked_attempt_stays_in_the_trajectory(d):
    """Visibility: the refused call is an execute_tool span with a failed status, so ExactOrder fails on it and
    InOrder tolerates it as an extra (release/trajectory.py, the offline twin of the service's evaluators)."""
    from module11.release import trajectory as T
    from module11.runtime.telemetry import RecordingBackend
    backend = RecordingBackend()
    doc = envelope(d)
    doc["request"]["change_summary"] = PLAN
    script = Script(calls(("delegate_recovery_planning", SVC)), calls(("get_deployment_history", SVC)),
                    calls(("delegate_recovery_planning", SVC)))
    runtime = make_runtime(d, model=F.FakeModelClient(script), backend=backend)
    body, _ = invoke(runtime, doc)
    spans = [{"name": s.name, "attributes": dict(s.attributes), "endTimeUnixNano": s.end}
             for s in backend.spans if s.trace_id == body["trace_id"]]
    blocked = [s for s in spans if s["attributes"].get("gen_ai.tool.name") == "delegate_recovery_planning"
               and s["attributes"].get("m11.tool_status") == "failed"]
    assert len(blocked) == 1 and "guard_blocked" in blocked[0]["attributes"]["gen_ai.tool.call.result"]
    actual = [t for t in T.tool_sequence(spans) if t in ("delegate_recovery_planning", "get_deployment_history")]
    assert actual == ["delegate_recovery_planning", "get_deployment_history", "delegate_recovery_planning"]
    expected = ["get_deployment_history", "delegate_recovery_planning"]
    assert T.exact_order(expected, actual) == 0.0
    assert T.in_order(expected, actual) == 1.0


# ---------------------------------------------------------------- guardrail
def test_guardrail_checks_input_then_every_output(d):
    guard = RecordingGuardrail()
    script = Script(calls(("get_deployment_history", SVC)), calls(("delegate_recovery_planning", SVC)))
    body, _ = run(d, script, guardrail=guard, change_summary="Checkout is down. Plan the recovery.")
    assert body["status"] == "completed"
    assert [source for source, _ in guard.calls] == ["INPUT", "OUTPUT", "OUTPUT"]
    assert guard.calls[0][1] == "Checkout is down. Plan the recovery."
    assert guard.calls[1][1] == body["recovery_plan"]["text"]           # the planner draft
    assert body["review"].startswith(guard.calls[2][1].rstrip())        # the final companion text


def test_input_intervention_blocks_before_any_model_call(d):
    script = Script()
    body, _ = run(d, script, guardrail=RecordingGuardrail(block="INPUT"))
    assert (body["status"], body["review"], body["error"]) == ("blocked", "Blocked by the demo guardrail.",
                                                               "Blocked by the demo guardrail.")
    assert script.requests == [] and body["m11"]["guardrail"]["source"] == "INPUT"


def test_output_intervention_blocks_the_review(d):
    body, _ = run(d, Script(calls(("get_deployment_history", SVC))), guardrail=RecordingGuardrail(block="OUTPUT"))
    assert body["status"] == "blocked" and body["m11"]["guardrail"] == {"action": G.INTERVENED, "source": "OUTPUT"}
    assert "Proposal" not in body["review"]


def test_guardrail_unavailable_fails_closed(d):
    body, _ = run(d, Script(calls(("get_deployment_history", SVC))), guardrail=RecordingGuardrail(fail=True))
    assert body["status"] == "failed" and body["detail"] == "guardrail_unavailable"
    assert "review" not in body


# ---------------------------------------------------------------- context.data (M6)
def test_context_data_is_passed_to_every_tool_call(d):
    invoker = RecordingInvoker()
    body, _ = run(d, F.FakeModelClient.default, invoker=invoker,
                  change_summary="Checkout 502s since the deploy. Plan the recovery.")
    assert body["status"] == "completed"
    data = config_doc(d)
    expected = {"snapshot": data["snapshot"], "evidence": data["evidence"], "memoryId": data["memoryId"],
                "artifactsBucket": data["artifactsBucket"]}
    arns = [arn for arn, _ in invoker.events]
    assert {"local:get_blast_radius", "local:check_rollback_readiness", "local:search_runbooks"} <= set(arns)
    assert all(event["context"]["data"] == expected for _, event in invoker.events)
    assert [event["context"]["sequence"] for _, event in invoker.events] == sorted(
        event["context"]["sequence"] for _, event in invoker.events)
