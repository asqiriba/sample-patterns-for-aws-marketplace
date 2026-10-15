# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bounded retry of the runtime's model calls (runtime/model_retry.py).

The policy and the loop's handling run everywhere. The SDK cases (the real `openai` retry loop against a mock
transport) need the runtime's pinned openai 3.24.0 and skip without it; run them with the runtime venv:
`.venvs/module11-runtime/bin/python -m pytest tests/test_module11_model_retry.py -p no:cacheprovider`."""
from __future__ import annotations

import io
import json
import time

import pytest

from module11.runtime import fake_model as F
from module11.runtime import model_retry as MR
from module11.runtime.tools import LambdaInvoker
from test_module11_loop import SVC, RecordingInvoker, Script, calls, d, run  # noqa: F401  (d is a fixture)


class InternalServerError(Exception):
    """The shape of openai.InternalServerError the loop sees (no openai import here)."""

    def __init__(self, status: int = 500) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status


class BadRequestError(Exception):
    def __init__(self) -> None:
        super().__init__("Error code: 400")
        self.status_code = 400


class Flaky:
    """A model client that simulates the SDK: `failures` provider errors on the first call that has tools, with
    a retry noted for each one up to MAX_RETRIES (what BoundedRetryOpenAI records), then the scripted reply."""

    def __init__(self, script, failures: int, error=InternalServerError, retry_class: str | None = "5xx"):
        self.inner, self.failures, self.error, self.retry_class = F.FakeModelClient(script), failures, error, retry_class
        self.calls = 0

    def create(self, **params):
        self.calls += 1
        if self.failures and self.calls == 1:
            if self.retry_class:                         # the SDK retries only retryable statuses
                for _ in range(min(self.failures, MR.MAX_RETRIES)):
                    MR.note_retry(self.retry_class, 500 if self.retry_class == "5xx" else 429, 0.5)
            if self.failures > MR.MAX_RETRIES or not self.retry_class:
                raise self.error()
        return self.inner.create(**params)


def _run(d, client, invoker=None):
    from test_module11_support import envelope, invoke, make_runtime
    runtime = make_runtime(d, model=client, invoker=invoker)
    return invoke(runtime, envelope(d))


# ---------------------------------------------------------------- policy
def test_only_provider_5xx_and_throttles_are_retryable():
    assert MR.classify(500) == MR.classify(503) == "5xx" and MR.classify(429) == "throttle"
    assert MR.classify(None) == "stream"
    for status in (400, 401, 403, 404, 408, 409, 413, 422):
        assert MR.classify(status) is None and status not in MR.RETRY_STATUS
    assert MR.MAX_RETRIES == 2


def test_a_retry_starts_only_with_time_left_before_the_deadline():
    now = 1000.0
    with MR.budget(now + 30.0):
        assert MR.allows(1.0, now=now)                                  # 1 + 10 <= 30
        assert not MR.allows(25.0, now=now)                             # 25 + 10 > 30
    with MR.budget(now + 10.5):
        assert not MR.allows(1.0, now=now)
    assert MR.allows(100.0, now=now)                                    # no budget: bounded by MAX_RETRIES only


def test_provider_errors_are_recognised_without_openai():
    assert MR.provider_error(InternalServerError()) == ("InternalServerError", 500)
    assert MR.provider_error(InternalServerError(429)) == ("InternalServerError", 429)
    assert MR.provider_error(BadRequestError()) is None
    assert MR.provider_error(ValueError("x")) is None
    assert MR.provider_error(MR.ModelRetryStopped("stream", 1)) == ("ModelRetryStopped", None)
    assert MR.detail_class("DevOps Companion: the model provider failed (InternalServerError HTTP 500) "
                           "after 2 retries") == "InternalServerError"


def test_the_metric_is_one_emf_line_per_class():
    out = io.StringIO()
    MR.emit_metric("staging", [{"class": "5xx"}, {"class": "5xx"}, {"class": "throttle"}], out=out, now_ms=1)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [(x["Class"], x["ModelRetries"]) for x in lines] == [("5xx", 2), ("throttle", 1)]
    assert lines[0]["_aws"]["CloudWatchMetrics"][0] == {"Namespace": "m11", "Dimensions": [["Stage", "Class"]],
                                                        "Metrics": [{"Name": "ModelRetries", "Unit": "Count"}]}
    out = io.StringIO()
    MR.emit_metric("staging", [], out=out)
    assert out.getvalue() == ""


# ---------------------------------------------------------------- the loop
def test_retried_then_success_completes_and_records_the_retries(d):
    from module11.runtime.telemetry import RecordingBackend
    from test_module11_support import envelope, invoke, make_runtime
    backend = RecordingBackend()
    client = Flaky(F.FakeModelClient.default, failures=2)
    body, _ = invoke(make_runtime(d, model=client, backend=backend), envelope(d))
    assert body["status"] == "completed"
    assert body["m11"]["modelRetries"] == {"5xx": 2, "throttle": 0, "stream": 0}
    chats = [s for s in backend.spans if s.attributes.get("m11.model_retries") is not None]
    assert chats and chats[0].attributes["m11.model_retries"] == 2
    assert chats[0].attributes["m11.model_retry_classes"] == "5xx,5xx"
    assert all(s.attributes["m11.model_retries"] == 0 for s in chats[1:])


def test_retries_exhausted_end_with_a_plain_answer_naming_the_failure(d):
    body, _ = _run(d, Flaky(F.FakeModelClient.default, failures=3))
    assert body["status"] == "failed"
    assert "the model provider failed (InternalServerError HTTP 500) after 2 retries" in body["detail"]
    assert body["review"] and body["review"].startswith("This investigation is incomplete: DevOps Companion: "
                                                        "the model provider failed (InternalServerError HTTP 500)")
    assert "Treat any cause above as unverified; no change was made." in body["review"]
    assert body["m11"]["modelRetries"]["5xx"] == 2


def test_a_validation_error_is_not_a_provider_failure(d):
    """A 4xx is a bug in the request, not a provider incident: no retry and no provider-failure answer."""
    body, _ = _run(d, Flaky(F.FakeModelClient.default, failures=1, error=BadRequestError, retry_class=None))
    assert body["status"] == "failed" and body["detail"] == "BadRequestError" and not body.get("review")
    assert body["m11"]["modelRetries"] == {"5xx": 0, "throttle": 0, "stream": 0}


def test_tool_calls_are_never_retried(d):
    """A model failure after a tool call ends the session; the tool ran exactly once. The Lambda invoker has
    no SDK retries."""
    invoker = RecordingInvoker()

    class AfterTool(Flaky):
        def create(self, **params):
            self.calls += 1
            if self.calls == 2:                         # the turn after get_deployment_history
                for _ in range(MR.MAX_RETRIES):
                    MR.note_retry("5xx", 503, 0.5)
                raise InternalServerError(503)
            return self.inner.create(**params)

    script = Script(calls(("get_deployment_history", SVC)))
    body, _ = _run(d, AfterTool(script, failures=0), invoker=invoker)
    assert body["status"] == "failed" and "HTTP 503) after 2 retries" in body["detail"]
    invoked = [arn for arn, _ in invoker.events]
    assert len(invoked) == 1 and "get_deployment_history" in invoked[0]
    assert "get_deployment_history" in body["review"]


def test_the_lambda_invoker_makes_one_attempt(monkeypatch):
    seen = {}

    def fake_client(name, config=None, **kw):
        seen["name"], seen["config"] = name, config
        return object()

    monkeypatch.setattr("module11.aws.client", fake_client)
    LambdaInvoker()._lambda()
    assert seen["name"] == "lambda" and seen["config"].retries == {"total_max_attempts": 1}


# ---------------------------------------------------------------- the SDK's retry loop (needs openai)
def _sdk(handler, max_retries=MR.MAX_RETRIES):
    openai = pytest.importorskip("openai")
    httpx2 = pytest.importorskip("httpx2")
    cls = MR.bounded_class(openai.OpenAI)
    return cls(api_key="test", base_url="https://bedrock.test/v1", max_retries=max_retries, timeout=30.0,
               http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def _ok():
    httpx2 = pytest.importorskip("httpx2")
    body = {"id": "resp_1", "object": "response", "created_at": 1, "model": "m", "status": "completed",
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
            "parallel_tool_calls": False, "tool_choice": "auto", "tools": []}
    return httpx2.Response(200, json=body)


def _status(code, headers=None):
    httpx2 = pytest.importorskip("httpx2")
    return httpx2.Response(code, json={"error": {"message": "x", "type": "server_error"}}, headers=headers or {})


def test_sdk_hooks_exist_in_the_pinned_openai():
    openai = pytest.importorskip("openai")
    assert openai.__version__ == "3.24.0"
    for hook in MR.OPENAI_HOOKS:
        assert callable(getattr(openai.OpenAI, hook, None)), hook


def test_sdk_retries_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    replies = [_status(500), _status(503), _ok()]
    seen = []

    def handler(request):
        seen.append(request.headers.get("x-stainless-retry-count"))
        return replies.pop(0)

    with MR.budget(time.monotonic() + 120) as b:
        out = _sdk(handler).responses.create(model="m", input="hi")
    assert out.output_text == "ok" and seen == ["0", "1", "2"]
    assert [r["class"] for r in b.retries] == ["5xx", "5xx"] and [r["status"] for r in b.retries] == [500, 503]


def test_sdk_gives_up_after_two_retries(monkeypatch):
    openai = pytest.importorskip("openai")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    count = []

    def handler(request):
        count.append(1)
        return _status(502)

    with MR.budget(time.monotonic() + 120) as b, pytest.raises(openai.InternalServerError):
        _sdk(handler).responses.create(model="m", input="hi")
    assert len(count) == 3 and len(b.retries) == 2


def test_sdk_never_retries_a_validation_error_or_other_4xx(monkeypatch):
    openai = pytest.importorskip("openai")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    for code in (400, 404, 408, 409, 422):
        count = []

        def handler(request, code=code):
            count.append(1)
            return _status(code)

        with MR.budget(time.monotonic() + 120) as b, pytest.raises(openai.APIStatusError):
            _sdk(handler).responses.create(model="m", input="hi")
        assert len(count) == 1 and b.retries == [], code


def test_sdk_throttles_are_retried_and_counted_separately(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    replies = [_status(429), _ok()]
    with MR.budget(time.monotonic() + 120) as b:
        _sdk(lambda request: replies.pop(0)).responses.create(model="m", input="hi")
    assert MR.counts(b.retries) == {"5xx": 0, "throttle": 1, "stream": 0}


def test_sdk_respects_the_session_deadline(monkeypatch):
    openai = pytest.importorskip("openai")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    count = []

    def handler(request):
        count.append(1)
        return _status(500)

    with MR.budget(time.monotonic() + MR.MIN_ATTEMPT_SECONDS) as b, pytest.raises(openai.InternalServerError):
        _sdk(handler).responses.create(model="m", input="hi")
    assert len(count) == 1 and b.retries == [] and b.stopped == "deadline"
    # A long Retry-After that would cross the deadline is not waited for either.
    count.clear()
    with MR.budget(time.monotonic() + 60) as b, pytest.raises(openai.InternalServerError):
        _sdk(lambda request: (count.append(1), _status(503, {"retry-after": "55"}))[1]).responses.create(
            model="m", input="hi")
    assert len(count) == 1 and b.stopped == "deadline"


def test_sdk_attempt_timeout_is_cut_to_the_deadline(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.extensions.get("timeout"))
        return _ok()

    with MR.budget(time.monotonic() + 20):
        _sdk(handler).responses.create(model="m", input="hi", timeout=60.0)
    read = seen[0]["read"] if isinstance(seen[0], dict) else None
    assert read is not None and read <= 20.0
