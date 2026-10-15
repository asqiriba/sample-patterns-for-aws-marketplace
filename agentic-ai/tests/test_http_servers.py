# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_http_servers.py
==========================
Offline hardening tests for the local demo HTTP servers of Modules 1-7, 9
and the routing agent.

Each server's handler class is served on 127.0.0.1 on a random port in a
background thread. The agent (or handler function) behind the POST endpoint
is replaced with a stub, so no model is called. The tests check that:

- GET /ping answers 200;
- POST requires Content-Type: application/json (415 otherwise);
- a Content-Length above 1 MiB is refused with 413 before the body is read;
- a missing or invalid Content-Length is refused with 400;
- a foreign Host header is refused with 403 (DNS-rebinding protection);
- OPTIONS is not answered with 2xx and no response carries CORS headers;
- an unexpected exception returns a generic 500 whose detail is only the
  exception type name;
- a valid JSON POST keeps its original success status and response shape.

Each module's heavy dependencies are imported with pytest.importorskip, so
one venv may skip servers another venv covers:

    core venv     module1, module2, module3, module5, module6, module9, routing
    module4 venv  module4      (pytest tests/test_http_servers.py -k module4)
    module7 venv  module7      (pytest tests/test_http_servers.py -k module7)

Run:
    pytest tests/test_http_servers.py -v -rs
"""

from __future__ import annotations

import http.client
import importlib
import json
import threading
from dataclasses import dataclass
from http.server import HTTPServer
from typing import Any, Callable

import pytest

MIB = 1024 * 1024
MOCK_FLAGS = (
    "AGENT_MOCK_AWS",
    "AGENT_MOCK_REPO",
    "AGENT_MOCK_DOMAIN",
    "AGENT_MOCK_MODE",
    "AGENT_MOCK_PIPELINE",
    "AGENT_MOCK_MEMORY",
)
SECRET = "secret-detail"  # nosec B105 - test sentinel that must never reach a response


class _Msg:
    """Minimal stand-in for a LangChain message."""

    def __init__(self, content: str) -> None:
        self.content = content


class _Recorder:
    """Records stub calls and either returns a canned value or raises."""

    def __init__(self, behaviour: str, value: Any) -> None:
        self.behaviour = behaviour
        self.value = value
        self.calls: list[tuple] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self.behaviour == "raise":
            raise RuntimeError(SECRET)
        return self.value


class _AgentStub:
    """Graph-like stub: .invoke() delegates to a recorder."""

    def __init__(self, recorder: _Recorder) -> None:
        self.invoke = recorder


# ---------------------------------------------------------------------------
# Per-server definitions
# ---------------------------------------------------------------------------

@dataclass
class ServerCase:
    module: str
    deps: tuple[str, ...]
    handler: Callable[[Any], type]
    install_stub: Callable[[pytest.MonkeyPatch, Any, str], _Recorder]
    post_path: str
    post_body: dict
    expected: dict


def _stub_module1(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"result": "ok", "region": "us-east-1", "mock_mode": True,
                                "model": "stub"})
    monkeypatch.setattr(mod, "_handle", rec)
    return rec


def _stub_module2(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"repo_path": "/repo", "analysis": "ok", "mock_mode": True,
                                "framework": "langchain"})
    monkeypatch.setattr(mod, "_handle_analysis", rec)
    return rec


def _stub_module3(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"output": "cdk-code"})
    monkeypatch.setattr(mod, "generate_infrastructure", rec)
    return rec


def _stub_module4(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"output": "plan"})
    monkeypatch.setattr(mod, "orchestrate_request", rec)
    return rec


def _stub_module5(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"messages": [_Msg("domain answer")]})

    class _DomainAgent:
        agent = _AgentStub(rec)

    monkeypatch.setattr(mod, "_get_agent", lambda domain: _DomainAgent())
    return rec


def _stub_module6(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"workflow_id": "wf-1", "status": "started"})

    class _Workflow:
        start = rec

    monkeypatch.setattr(mod, "_get_workflow", lambda: _Workflow())
    return rec


def _stub_module7(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"messages": [_Msg("memory answer")]})
    monkeypatch.setattr(mod, "_get_agent", lambda: _AgentStub(rec))
    return rec


def _stub_module9(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"messages": [_Msg("pipeline answer")]})
    monkeypatch.setattr(mod, "_get_agent", lambda: (_AgentStub(rec), "sid-1"))
    return rec


def _stub_routing(monkeypatch, mod, behaviour):
    rec = _Recorder(behaviour, {"category": "repository_analysis", "confidence": 0.9})
    monkeypatch.setattr(mod, "route_request", rec)
    return rec


LANGCHAIN = ("langchain_aws", "langgraph")

CASES = {
    "module1": ServerCase(
        module="module1.app",
        deps=("strands",),
        handler=lambda mod: mod.make_handler(),
        install_stub=_stub_module1,
        post_path="/invocations",
        post_body={"prompt": "Give me a health summary of us-east-1"},
        expected={"result": "ok", "region": "us-east-1", "mock_mode": True, "model": "stub"},
    ),
    "module2": ServerCase(
        module="module2.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Handler,
        install_stub=_stub_module2,
        post_path="/analyze",
        post_body={"repo_path": "/repo"},
        expected={"repo_path": "/repo", "analysis": "ok", "mock_mode": True,
                  "framework": "langchain"},
    ),
    "module3": ServerCase(
        module="module3.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Module3Handler,
        install_stub=_stub_module3,
        post_path="/generate",
        post_body={"requirements": {"compute": "ECS"}},
        expected={"status": "success", "region": "us-east-1", "environment": "dev",
                  "output": "cdk-code"},
    ),
    "module4": ServerCase(
        module="module4.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Module4Handler,
        install_stub=_stub_module4,
        post_path="/orchestrate",
        post_body={"request": "Analyze my repo"},
        expected={"status": "success", "request": "Analyze my repo", "output": "plan"},
    ),
    "module5": ServerCase(
        module="module5.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Module5Handler,
        install_stub=_stub_module5,
        post_path="/invoke",
        post_body={"domain": "customer", "message": "Find me a monitoring tool"},
        expected={"status": "success", "domain": "customer", "output": "domain answer"},
    ),
    "module6": ServerCase(
        module="module6.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Module6Handler,
        install_stub=_stub_module6,
        post_path="/start",
        post_body={"repo_path": "/app/nodejs-api"},
        expected={"workflow_id": "wf-1", "status": "started"},
    ),
    "module7": ServerCase(
        module="module7.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Module7Handler,
        install_stub=_stub_module7,
        post_path="/invoke",
        post_body={"query": "What happened to notification-svc?"},
        expected={"response": "memory answer", "tool_calls": [],
                  "session_id": "http-default-session"},
    ),
    "module9": ServerCase(
        module="module9.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.Module9Handler,
        install_stub=_stub_module9,
        post_path="/invoke",
        post_body={"prompt": "What changed in the last deployment?"},
        expected={"response": "pipeline answer", "session_id": "sid-1"},
    ),
    "routing": ServerCase(
        module="routing_agent.app",
        deps=LANGCHAIN,
        handler=lambda mod: mod.RoutingHandler,
        install_stub=_stub_routing,
        post_path="/route",
        post_body={"request": "Analyze my repository"},
        expected={"status": "success",
                  "routing": {"category": "repository_analysis", "confidence": 0.9}},
    ),
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _load_app(case: ServerCase, monkeypatch: pytest.MonkeyPatch):
    for dep in case.deps:
        pytest.importorskip(dep, reason=f"{case.module} needs {dep} (run it in its own venv)")
    for flag in MOCK_FLAGS:
        monkeypatch.setenv(flag, "true")
    return importlib.import_module(case.module)


class _Running:
    def __init__(self, server: HTTPServer, recorder: _Recorder, case: ServerCase, mod: Any):
        self.server = server
        self.recorder = recorder
        self.case = case
        self.mod = mod
        self.port = server.server_address[1]

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        host: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Send a raw request with exactly the given headers."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        finally:
            conn.close()

    def post_json(self, body: dict | None = None, **kwargs) -> tuple[int, dict[str, str], bytes]:
        raw = json.dumps(self.case.post_body if body is None else body).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(raw))}
        return self.request("POST", self.case.post_path, raw, headers, **kwargs)


def _serve(case_id: str, behaviour: str, monkeypatch: pytest.MonkeyPatch):
    case = CASES[case_id]
    mod = _load_app(case, monkeypatch)
    recorder = case.install_stub(monkeypatch, mod, behaviour)
    server = HTTPServer(("127.0.0.1", 0), case.handler(mod))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread, _Running(server, recorder, case, mod)


@pytest.fixture(params=list(CASES), ids=list(CASES))
def running(request, monkeypatch):
    """A server whose stub returns the canned success value."""
    server, thread, run = _serve(request.param, "ok", monkeypatch)
    yield run
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(params=list(CASES), ids=list(CASES))
def failing(request, monkeypatch):
    """A server whose stub raises RuntimeError('secret-detail')."""
    server, thread, run = _serve(request.param, "raise", monkeypatch)
    yield run
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _assert_no_cors(headers: dict[str, str]) -> None:
    assert not any(name.startswith("access-control-allow") for name in headers), headers


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ping_ok(running):
    status, headers, _ = running.request("GET", "/ping")
    assert status == 200
    _assert_no_cors(headers)


def test_valid_json_post_keeps_success_shape(running):
    status, headers, body = running.post_json()
    assert status == 200, body
    assert json.loads(body) == running.case.expected
    assert len(running.recorder.calls) == 1
    _assert_no_cors(headers)


def test_non_json_content_type_is_415(running):
    raw = b"x"
    status, headers, _ = running.request(
        "POST", running.case.post_path, raw,
        {"Content-Type": "text/plain", "Content-Length": str(len(raw))},
    )
    assert status == 415
    assert running.recorder.calls == []
    _assert_no_cors(headers)


def test_missing_content_type_is_415(running):
    raw = json.dumps(running.case.post_body).encode()
    status, _, _ = running.request(
        "POST", running.case.post_path, raw, {"Content-Length": str(len(raw))}
    )
    assert status == 415
    assert running.recorder.calls == []


def test_json_content_type_with_charset_is_accepted(running):
    raw = json.dumps(running.case.post_body).encode()
    status, _, body = running.request(
        "POST", running.case.post_path, raw,
        {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(raw))},
    )
    assert status == 200, body


def test_oversized_content_length_is_413(running):
    # Headers only: the server must refuse without reading a body.
    status, _, _ = running.request(
        "POST", running.case.post_path, None,
        {"Content-Type": "application/json", "Content-Length": str(MIB + 1)},
    )
    assert status == 413
    assert running.recorder.calls == []


@pytest.mark.parametrize("length", ["abc", "-1", "1.5"])
def test_invalid_content_length_is_400(running, length):
    status, _, _ = running.request(
        "POST", running.case.post_path, None,
        {"Content-Type": "application/json", "Content-Length": length},
    )
    assert status == 400
    assert running.recorder.calls == []


def test_missing_content_length_is_400(running):
    status, _, _ = running.request(
        "POST", running.case.post_path, None, {"Content-Type": "application/json"}
    )
    assert status == 400
    assert running.recorder.calls == []


def test_invalid_json_is_400(running):
    raw = b"{not json"
    status, _, _ = running.request(
        "POST", running.case.post_path, raw,
        {"Content-Type": "application/json", "Content-Length": str(len(raw))},
    )
    assert status == 400
    assert running.recorder.calls == []


def test_json_array_body_is_400(running):
    raw = b"[1, 2]"
    status, _, _ = running.request(
        "POST", running.case.post_path, raw,
        {"Content-Type": "application/json", "Content-Length": str(len(raw))},
    )
    assert status == 400
    assert running.recorder.calls == []


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_foreign_host_is_403(running, method):
    if method == "GET":
        status, _, body = running.request("GET", "/ping", host="evil.example")
    else:
        # Headers only: the Host check runs before the body is read, so an
        # unread body could turn the 403 into a connection reset.
        raw = json.dumps(running.case.post_body).encode()
        status, _, body = running.request(
            "POST", running.case.post_path, None,
            {"Content-Type": "application/json", "Content-Length": str(len(raw))},
            host="evil.example",
        )
    assert status == 403
    assert json.loads(body) == {"error": "forbidden host"}
    assert running.recorder.calls == []


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:8080", "127.0.0.1", "[::1]:9999"])
def test_loopback_host_names_are_allowed(running, host):
    status, _, _ = running.request("GET", "/ping", host=host)
    assert status == 200


def test_host_with_loopback_prefix_is_refused(running):
    status, _, _ = running.request("GET", "/ping", host="127.0.0.1.evil.example")
    assert status == 403


def test_options_is_not_a_cors_preflight(running):
    status, headers, _ = running.request(
        "OPTIONS", running.case.post_path, None,
        {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert not 200 <= status < 300
    _assert_no_cors(headers)


def test_unexpected_exception_is_generic_500(failing, capsys):
    status, headers, body = failing.post_json()
    assert status == 500
    assert SECRET.encode() not in body
    data = json.loads(body)
    assert data == {"error": "internal server error", "detail": "RuntimeError"}
    _assert_no_cors(headers)


def test_loopback_detection(running):
    is_loopback = running.mod._is_loopback
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")  # nosec B104 - asserts the bind rule; nothing binds
    assert not is_loopback("192.0.2.10")


# ---------------------------------------------------------------------------
# Module 2: /analyze sends the repo path to the LangGraph agent as a message
# ---------------------------------------------------------------------------

def test_module2_analysis_uses_messages_input(monkeypatch):
    mod = _load_app(CASES["module2"], monkeypatch)
    rec = _Recorder("ok", {"messages": [_Msg("user turn"), _Msg("two services found")]})
    monkeypatch.setattr(mod, "_agent", _AgentStub(rec))
    result = mod._handle_analysis({"repo_path": "/repo"})
    (agent_input,), _ = rec.calls[0]
    assert agent_input == {"messages": [("user", "Analyze the git repository at: /repo")]}
    assert result == {"repo_path": "/repo", "analysis": "two services found",
                      "mock_mode": True, "framework": "langchain"}


# ---------------------------------------------------------------------------
# Module 1: request-supplied Region validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "region", ["evil;rm -rf", "US-EAST-1", "us-east-1\n", "us-east-\u0661", "x" * 50, 42]
)
def test_module1_rejects_invalid_region(monkeypatch, region):
    case = CASES["module1"]
    mod = _load_app(case, monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    result = mod._handle({"prompt": "hello", "region": region})
    assert result == {"error": "invalid region"}
    import os

    assert os.environ["AWS_REGION"] == "us-east-1"


def test_module1_invalid_region_is_http_400(monkeypatch):
    case = CASES["module1"]
    mod = _load_app(case, monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    server = HTTPServer(("127.0.0.1", 0), mod.make_handler())
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        run = _Running(server, _Recorder("ok", None), case, mod)
        status, _, body = run.post_json({"prompt": "hello", "region": "bad region"})
        assert status == 400
        assert json.loads(body) == {"error": "invalid region"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-2", "ap-southeast-3", "us-gov-west-1"])
def test_module1_region_pattern_accepts_real_regions(monkeypatch, region):
    mod = _load_app(CASES["module1"], monkeypatch)
    assert mod._REGION_RE.fullmatch(region)
