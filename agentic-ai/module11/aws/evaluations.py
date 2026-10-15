# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The AgentCore Evaluations adapter.

One `Evaluate` call per evaluator and per session (the API takes one session per call):
- session-level evaluators (`Builtin.GoalSuccessRate`, the `Trajectory*` set, the code-based evaluator)
  send no `evaluationTarget`;
- trace-level evaluators (`Builtin.Correctness`) send `evaluationTarget.traceIds`.
`references()` builds `evaluationReferenceInputs` from the scenario, one ground-truth form per evaluator:
`assertions` (goal success), `expectedResponse` scoped by `context.spanContext` to the trace, and
`expectedTrajectory.toolNames` (the trajectory evaluators). Partial failures come back as results with an
`errorCode`; they become `value=None` with that code (gate rule 1), and the call is retried once.
Throttling backs off with jitter and gives up with `ThrottlingException` as the error code.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

GOAL = "Builtin.GoalSuccessRate"
CORRECTNESS = "Builtin.Correctness"
TRAJECTORY = ("Builtin.TrajectoryInOrderMatch", "Builtin.TrajectoryExactOrderMatch", "Builtin.TrajectoryAnyOrderMatch")
THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException"}
MAX_THROTTLE_ATTEMPTS = 6


@dataclass
class Outcome:
    """One evaluator's verdict on one session."""
    evaluator: str
    value: float | None
    label: str | None = None
    explanation: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    request_ids: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)       # the service's result entries, unaltered
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.value is not None and self.error_code is None


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) or type(exc).__name__


def references(evaluator_id: str, scenario: Mapping[str, Any], session_id: str,
               trace_id: str | None = None) -> list[dict] | None:
    """`evaluationReferenceInputs` for one evaluator, or None when it takes no ground truth."""
    session = {"spanContext": {"sessionId": session_id}}
    if evaluator_id == GOAL:
        assertions = [a for a in scenario.get("assertions") or [] if isinstance(a, str) and a.strip()]
        return [{"context": session, "assertions": [{"text": a} for a in assertions]}] if assertions else None
    if evaluator_id == CORRECTNESS:
        text = scenario.get("expected_response")
        if not text or not trace_id:
            return None
        return [{"context": {"spanContext": {"sessionId": session_id, "traceId": trace_id}},
                 "expectedResponse": {"text": text}}]
    if evaluator_id in TRAJECTORY:
        return [{"context": session, "expectedTrajectory": {"toolNames": list(scenario.get("expected_trajectory") or [])}}]
    return None


def request_for(evaluator_id: str, spans: Sequence[Mapping[str, Any]], *, level: str,
                trace_ids: Sequence[str] | None = None, refs: list[dict] | None = None) -> dict:
    """The keyword arguments of `Evaluate` (level: `session` or `trace`)."""
    request: dict[str, Any] = {"evaluatorId": evaluator_id, "evaluationInput": {"sessionSpans": list(spans)}}
    if level == "trace":
        if not trace_ids:
            raise ValueError(f"{evaluator_id} is trace level and needs trace IDs")
        request["evaluationTarget"] = {"traceIds": list(trace_ids)[:10]}
    elif level != "session":
        raise ValueError(f"unknown evaluator level {level!r}")
    if refs:
        request["evaluationReferenceInputs"] = refs
    return request


def _outcome(evaluator_id: str, response: Mapping[str, Any], request_id: str | None) -> Outcome:
    results = [dict(r) for r in response.get("evaluationResults") or []]
    out = Outcome(evaluator_id, None, request_ids=[request_id] if request_id else [], results=results)
    for r in results:
        usage = r.get("tokenUsage") or {}
        out.input_tokens += int(usage.get("inputTokens") or 0)
        out.output_tokens += int(usage.get("outputTokens") or 0)
    errors = [r for r in results if r.get("errorCode")]
    if errors:
        out.error_code = str(errors[0]["errorCode"])
        out.error_message = str(errors[0].get("errorMessage") or "")[:300]
        return out
    values = [float(r["value"]) for r in results if r.get("value") is not None]
    if not values:
        out.error_code = "no_value" if results else "no_results"
        return out
    out.value = sum(values) / len(values)
    out.label = next((r.get("label") for r in results if r.get("label")), None)
    out.explanation = next((r.get("explanation") for r in results if r.get("explanation")), None)
    return out


def evaluate_once(dp: Any, request: Mapping[str, Any], *, sleep: Callable[[float], None] = time.sleep,
                  rng: Callable[[], float] = random.random) -> Outcome:
    """One `Evaluate` call. Throttling backs off and retries up to six times; any other API error is an outcome
    with its error code, so one bad call never ends a run."""
    evaluator_id = str(request["evaluatorId"])
    delay = 2.0
    for attempt in range(1, MAX_THROTTLE_ATTEMPTS + 1):
        try:
            response = dp.evaluate(**request)
        except Exception as exc:
            code = _code(exc)
            if code in THROTTLE_CODES and attempt < MAX_THROTTLE_ATTEMPTS:
                sleep(delay * (0.5 + rng()))
                delay = min(delay * 2, 60.0)
                continue
            return Outcome(evaluator_id, None, error_code=code, error_message=str(exc)[:600], attempts=attempt)
        request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
        out = _outcome(evaluator_id, response, request_id)
        out.attempts = attempt
        return out
    raise AssertionError("unreachable")


def evaluate(dp: Any, request: Mapping[str, Any], *, retries: int = 1, sleep: Callable[[float], None] = time.sleep,
             rng: Callable[[], float] = random.random) -> Outcome:
    """`Evaluate` with one retry for a failed or empty evaluation (gate rule 1). Token usage of every
    attempt is counted, because the service bills a failed judge call too."""
    first = evaluate_once(dp, request, sleep=sleep, rng=rng)
    outcome = first
    for _ in range(retries):
        if outcome.ok:
            break
        again = evaluate_once(dp, request, sleep=sleep, rng=rng)
        again.input_tokens += outcome.input_tokens
        again.output_tokens += outcome.output_tokens
        again.request_ids = outcome.request_ids + again.request_ids
        again.attempts += outcome.attempts
        outcome = again
    return outcome


def blocked_outcome(evaluator_id: str) -> Outcome:
    """An adversarial input that the guardrail stopped is the designed outcome: the session has no tool
    calls to judge, so each judged evaluator records a pass labelled as such."""
    return Outcome(evaluator_id, 1.0, label="BLOCKED_BY_GUARDRAIL",
                   explanation="the guardrail stopped this adversarial input before any tool ran; no judge call made")
