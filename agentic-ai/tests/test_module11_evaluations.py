# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Evaluations adapter: request shapes per evaluator level, the three
ground-truth forms, partial failures as `value None` + error code, one retry, throttling backoff."""
import pytest
from botocore.exceptions import ClientError

from module11.aws import evaluations as EV

SID = "m11-eval-0a1b2c3d4e5f-incident-triage-014-1-1b4e2c3d"
SCENARIO = {"scenario_id": "incident-triage-014", "assertions": ["names d-4471", "quotes a log line"],
            "expected_response": "d-4471 at 14:20", "expected_trajectory": ["get_deployment_history", "query_metrics"]}
SPANS = [{"traceId": "t1", "name": "invoke_agent x"}]


def result(value=1.0, **extra):
    return {"evaluatorId": "Builtin.GoalSuccessRate", "value": value, "label": "Yes", "explanation": "ok",
            "tokenUsage": {"inputTokens": 100, "outputTokens": 10}, **extra}


class FakeDP:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def evaluate(self, **kw):
        self.calls.append(kw)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return {"ResponseMetadata": {"RequestId": f"req-{len(self.calls)}"}, "evaluationResults": item}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Evaluate")


def test_the_three_ground_truth_forms():
    goal = EV.references("Builtin.GoalSuccessRate", SCENARIO, SID)
    assert goal == [{"context": {"spanContext": {"sessionId": SID}},
                     "assertions": [{"text": "names d-4471"}, {"text": "quotes a log line"}]}]
    correct = EV.references("Builtin.Correctness", SCENARIO, SID, "t1")
    assert correct == [{"context": {"spanContext": {"sessionId": SID, "traceId": "t1"}},
                        "expectedResponse": {"text": "d-4471 at 14:20"}}]
    for name in ("Builtin.TrajectoryInOrderMatch", "Builtin.TrajectoryExactOrderMatch"):
        assert EV.references(name, SCENARIO, SID) == [
            {"context": {"spanContext": {"sessionId": SID}},
             "expectedTrajectory": {"toolNames": ["get_deployment_history", "query_metrics"]}}]
    assert EV.references("custom.no_credential_leakage", SCENARIO, SID) is None
    assert EV.references("Builtin.Correctness", SCENARIO, SID, None) is None       # scoped to a trace or nothing


def test_an_empty_expected_trajectory_is_still_a_reference():
    """The API accepts toolNames with zero entries, so a scenario that expects no tool calls is scored."""
    refs = EV.references("Builtin.TrajectoryInOrderMatch", {**SCENARIO, "expected_trajectory": []}, SID)
    assert refs[0]["expectedTrajectory"] == {"toolNames": []}


def test_session_level_sends_no_target_and_trace_level_sends_trace_ids():
    session = EV.request_for("Builtin.GoalSuccessRate", SPANS, level="session", refs=[{"context": {}}])
    assert "evaluationTarget" not in session and session["evaluationInput"] == {"sessionSpans": SPANS}
    assert session["evaluationReferenceInputs"] == [{"context": {}}]
    trace = EV.request_for("Builtin.Correctness", SPANS, level="trace", trace_ids=[f"t{i}" for i in range(12)])
    assert trace["evaluationTarget"] == {"traceIds": [f"t{i}" for i in range(10)]}           # 1-10
    with pytest.raises(ValueError):
        EV.request_for("Builtin.Correctness", SPANS, level="trace")
    with pytest.raises(ValueError):
        EV.request_for("x", SPANS, level="tool")


def test_a_good_result_and_token_counts():
    out = EV.evaluate(FakeDP([result(0.5), result(1.0)]), {"evaluatorId": "Builtin.Correctness"})
    assert out.ok and out.value == 0.75 and out.label == "Yes" and out.input_tokens == 200 and out.attempts == 1


def test_partial_failure_becomes_none_with_the_error_code_after_one_retry():
    dp = FakeDP([{"evaluatorId": "x", "errorCode": "ModelError", "errorMessage": "judge failed",
                  "tokenUsage": {"inputTokens": 50, "outputTokens": 1}}])
    out = EV.evaluate(dp, {"evaluatorId": "Builtin.Correctness"})
    assert out.value is None and out.error_code == "ModelError" and not out.ok
    assert len(dp.calls) == 2 and out.attempts == 2 and out.input_tokens == 100         # the retry, and both attempts billed


def test_the_retry_can_recover():
    dp = FakeDP([{"errorCode": "ModelError", "errorMessage": "x"}], [result(1.0)])
    out = EV.evaluate(dp, {"evaluatorId": "Builtin.Correctness"})
    assert out.ok and out.value == 1.0 and len(dp.calls) == 2 and out.request_ids == ["req-1", "req-2"]


def test_an_empty_result_list_is_an_error_not_a_pass():
    out = EV.evaluate(FakeDP([]), {"evaluatorId": "Builtin.Correctness"}, retries=0)
    assert out.value is None and out.error_code == "no_results"


def test_api_errors_are_outcomes_and_throttling_backs_off():
    dp = FakeDP(client_error("ValidationException"))
    out = EV.evaluate(dp, {"evaluatorId": "x"}, retries=0)
    assert out.error_code == "ValidationException" and len(dp.calls) == 1
    slept = []
    dp = FakeDP(client_error("ThrottlingException"), client_error("ThrottlingException"), [result(1.0)])
    out = EV.evaluate_once(dp, {"evaluatorId": "x"}, sleep=slept.append, rng=lambda: 0.5)
    assert out.ok and len(slept) == 2 and slept[1] > slept[0]
    dp = FakeDP(client_error("ThrottlingException"))
    out = EV.evaluate_once(dp, {"evaluatorId": "x"}, sleep=lambda s: None)
    assert out.error_code == "ThrottlingException" and len(dp.calls) == EV.MAX_THROTTLE_ATTEMPTS


def test_blocked_outcome_is_a_labelled_pass():
    out = EV.blocked_outcome("Builtin.GoalSuccessRate")
    assert out.value == 1.0 and out.label == "BLOCKED_BY_GUARDRAIL" and out.ok
