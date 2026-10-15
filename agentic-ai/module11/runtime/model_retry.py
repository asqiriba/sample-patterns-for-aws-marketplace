# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounded retry of the runtime's model calls.

Module 11 calls Bedrock directly through the OpenAI SDK's Bedrock provider (Portkey is not on this
path), so the retry is the SDK's own `max_retries` loop with a narrower policy. `client()` builds a subclass of
`openai.OpenAI` that overrides three of the SDK's retry hooks (pinned openai 3.24.0, checked by a test):

- `_should_retry`: retry only HTTP 500, 502, 503, 504 (provider 5xx) and 429 (throttle). Never retry another
  4xx: a validation error fails at once. An explicit `x-should-retry: false` from the service is obeyed.
- `_sleep_for_retry`: the SDK's exponential backoff with jitter (0.5 s, then 1 s, each minus up to 25%; a
  `Retry-After` up to 120 s is honored). Each retry is recorded with its class: `5xx`, `throttle`, or `stream`
  for a connection that failed or timed out before a response arrived.
- `_prepare_options`: each attempt's timeout is cut to the time left before the session deadline.

At most MAX_RETRIES retries per model call. A retry starts only if, after its backoff, at least
MIN_ATTEMPT_SECONDS of the session deadline remain; otherwise the original error is raised (`budget.stopped`
records why). Only model invocations go through this client. Tool calls never do: the Lambda invoker runs
with `total_max_attempts: 1` and the loop dispatches each tool call once.

The model call is a complete (non-streamed) Responses API request, and nothing from it reaches the caller
until it returns, so a retry cannot duplicate output. Events already sent for earlier tool calls are not sent
again. The retries of each call go on its `chat` span (`m11.model_retries`, `m11.model_retry_classes`), into
the result's `m11.modelRetries` and the gate report's provider-health block, and into the CloudWatch metric
`m11/ModelRetries` (dimensions Stage, Class) through an embedded-metric-format line on stdout.

`openai` is imported only inside `client()`, so this module imports without it (M13).
"""
from __future__ import annotations

import json
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

MAX_RETRIES = 2
SERVER_STATUS = frozenset({500, 502, 503, 504})
THROTTLE_STATUS = frozenset({429})
RETRY_STATUS = SERVER_STATUS | THROTTLE_STATUS
MIN_ATTEMPT_SECONDS = 10.0
BACKOFF_BOUND_SECONDS = (0.5, 1.0)        # the SDK's INITIAL_RETRY_DELAY x 2^n before jitter, n = 0, 1
CLASSES = ("5xx", "throttle", "stream")
METRIC_NAMESPACE, METRIC_NAME = "m11", "ModelRetries"
OPENAI_HOOKS = ("_should_retry", "_sleep_for_retry", "_prepare_options", "_calculate_retry_timeout",
                "_parse_retry_after_header")
PROVIDER_ERROR = re.compile(r"model provider failed \((?P<cls>[A-Za-z]+)")


class ModelRetryStopped(Exception):
    """A connection-level model failure that was not retried because the session deadline was too close."""

    def __init__(self, kind: str, retries: int) -> None:
        super().__init__(f"{kind} error with {retries} retries; the session deadline leaves no time for another")
        self.kind, self.retries = kind, retries


@dataclass
class Budget:
    deadline: float                                   # time.monotonic() value
    retries: list[dict] = field(default_factory=list)
    stopped: str | None = None                        # why a retryable error was not retried


_BUDGET: ContextVar[Budget | None] = ContextVar("m11_model_retry_budget", default=None)


@contextmanager
def budget(deadline: float) -> Iterator[Budget]:
    """The retry budget of one model call (the caller's thread and context only)."""
    current = Budget(deadline)
    token = _BUDGET.set(current)
    try:
        yield current
    finally:
        _BUDGET.reset(token)


def classify(status: int | None) -> str | None:
    if status is None:
        return "stream"
    if status in THROTTLE_STATUS:
        return "throttle"
    if status in SERVER_STATUS:
        return "5xx"
    return None


def allows(delay: float, now: float | None = None) -> bool:
    """True if a retry after `delay` seconds still leaves MIN_ATTEMPT_SECONDS before the deadline."""
    current = _BUDGET.get()
    if current is None:
        return True
    return (time.monotonic() if now is None else now) + delay + MIN_ATTEMPT_SECONDS <= current.deadline


def note_retry(kind: str, status: int | None, delay: float) -> None:
    current = _BUDGET.get()
    if current is not None:
        current.retries.append({"attempt": len(current.retries) + 1, "class": kind, "status": status,
                                "delayMs": round(delay * 1000.0, 1)})


def note_stopped(reason: str) -> None:
    current = _BUDGET.get()
    if current is not None:
        current.stopped = reason


def retries_so_far() -> int:
    current = _BUDGET.get()
    return len(current.retries) if current is not None else 0


def provider_error(exc: BaseException) -> tuple[str, int | None] | None:
    """(error class name, HTTP status) when `exc` is a model-provider failure, else None. Duck-typed so the
    loop needs no openai import: an SDK status error carries `status_code`."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status in RETRY_STATUS or status >= 500):
        return name, status
    if name in ("APIConnectionError", "APITimeoutError", "ModelRetryStopped"):
        return name, None
    return None


def counts(retries: list[dict]) -> dict[str, int]:
    out = {c: 0 for c in CLASSES}
    for r in retries:
        out[r["class"]] = out.get(r["class"], 0) + 1
    return out


def detail_class(detail: str | None) -> str | None:
    """The provider error class named in a session's failure detail (`... model provider failed (<Class> ...`)."""
    match = PROVIDER_ERROR.search(detail or "")
    return match.group("cls") if match else None


def emit_metric(stage: str, retries: list[dict], out: Any = None, now_ms: int | None = None) -> None:
    """One embedded-metric-format line per retry class with retries (CloudWatch extracts m11/ModelRetries)."""
    out = out or sys.stdout
    for kind, n in counts(retries).items():
        if not n:
            continue
        doc = {"_aws": {"Timestamp": int(time.time() * 1000) if now_ms is None else now_ms,
                        "CloudWatchMetrics": [{"Namespace": METRIC_NAMESPACE, "Dimensions": [["Stage", "Class"]],
                                               "Metrics": [{"Name": METRIC_NAME, "Unit": "Count"}]}]},
               "event": "model_retry", "Stage": stage, "Class": kind, METRIC_NAME: n}
        print(json.dumps(doc, sort_keys=True), file=out, flush=True)


def client(timeout: float = 60.0) -> Any:
    """The runtime's model client: the SDK's Bedrock provider with the bounded retry policy above."""
    from openai import OpenAI
    from openai.providers import bedrock
    return bounded_class(OpenAI)(provider=bedrock(endpoint="runtime", api_key=None, base_url=None),
                                 timeout=timeout, max_retries=MAX_RETRIES)


def bounded_class(base: type) -> type:
    """`base` (openai.OpenAI) with the narrowed retry hooks. Separate from `client()` so tests can build one
    against a mock transport."""

    class BoundedRetryOpenAI(base):  # type: ignore[misc, valid-type]
        def _should_retry(self, response: Any) -> bool:
            status = int(response.status_code)
            if status not in RETRY_STATUS or not super()._should_retry(response):
                return False
            retry_after = self._parse_retry_after_header(response.headers)
            n = retries_so_far()
            delay = retry_after if retry_after is not None and 0 < retry_after <= 120 else \
                BACKOFF_BOUND_SECONDS[min(n, len(BACKOFF_BOUND_SECONDS) - 1)]
            if not allows(delay):
                note_stopped("deadline")
                return False
            return True

        def _sleep_for_retry(self, *, retries_taken: int, max_retries: int, options: Any, response: Any) -> None:
            status = int(response.status_code) if response is not None else None
            kind = classify(status) or "5xx"
            delay = self._calculate_retry_timeout(max_retries - retries_taken, options,
                                                  response.headers if response is not None else None)
            if not allows(delay):
                note_stopped("deadline")
                raise ModelRetryStopped(kind, retries_so_far())
            note_retry(kind, status, delay)
            time.sleep(delay)

        def _prepare_options(self, options: Any) -> Any:
            options = super()._prepare_options(options)
            current = _BUDGET.get()
            if current is not None and isinstance(options.timeout, (int, float)):
                options.timeout = max(1.0, min(float(options.timeout), current.deadline - time.monotonic()))
            return options

    BoundedRetryOpenAI.__name__ = BoundedRetryOpenAI.__qualname__ = "BoundedRetryOpenAI"
    return BoundedRetryOpenAI
