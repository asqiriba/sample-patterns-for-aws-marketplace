"""Real Portkey + real SDK + real tool execution, with an unpaid test upstream."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import botocore.session
from botocore.credentials import ReadOnlyCredentials
import httpx2
from openai import OpenAI
import pytest

from module10.agent_runner import AgentRun
from module10.flags import DEFAULT_FLAGS_PATH, set_recovery_planning_enabled
from module10.gateway.setup import SOURCE
from module10.portkey_local import LocalPortkeyProcess, PortkeyReviewer
from module10.recovery import PLANNER_PROMPT
from module10.reviewer import ReviewService
from module10.runtime import RuntimeBundle
from module10.telemetry import TraceRecorder
from test_module10_reviews import (
    launch, investigate, response_payload, scripted_agent, wait_for_planning,
)

LUNA = "us.openai.gpt-5.6-luna"
TERRA = "us.openai.gpt-5.6-terra"


def drafting(payload):
    return payload["instructions"] == PLANNER_PROMPT and sum(
        item.get("type") == "function_call_output" for item in payload["input"]
    ) == 2


@pytest.fixture
def routed(monkeypatch, tmp_path, launch, request):
    if not (SOURCE / "node_modules/tsx/dist/loader.mjs").exists():
        pytest.fail("Install the pinned gateway first: .venv/bin/python module10/gateway/setup.py")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE_TEST_ACCESS")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FAKE_TEST_SECRET")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FAKE_TEST_TOKEN")
    monkeypatch.setenv("AWS_REGION", "us-test-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    state = SimpleNamespace(
        requests=[], status_for=lambda _: 200, delay_for=lambda _: 0,
        respond=scripted_agent, plans=tmp_path / "plans", delay_body=False,
    )

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(request)
            payload = request["payload"]
            if not state.delay_body:
                time.sleep(state.delay_for(payload))
            code = state.status_for(payload)
            if code == 200:
                body = state.respond(payload)
                body["model"] = payload["model"]
                body["id"] = f"response-{len(state.requests)}"
            else:
                body = {"error": {"message": "Test upstream failure", "type": "test_failure"}}
            encoded = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            if state.delay_body:
                time.sleep(state.delay_for(payload))
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the real gateway times out an attempt.

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MODULE10_TEST_UPSTREAM", f"http://127.0.0.1:{upstream.server_port}")
    remote = getattr(request, "param", "") == "remote"
    if remote:
        monkeypatch.setenv("MODULE10_TEST_EVIDENCE", str(tmp_path / "evidence"))
    gateway = LocalPortkeyProcess(Path(__file__).with_name("portkey_test_launcher.mjs"))
    client = OpenAI(
        api_key="s" * 48 if remote else "local-demo", base_url=gateway.base_url + "/v1/openai/v1",
        max_retries=0, http_client=httpx2.Client(trust_env=False),
    )
    if remote:
        from module10.portkey_remote import RemotePortkeyReviewer
        import io
        s3 = SimpleNamespace(get_object=lambda **kw: {
            "Body": io.BytesIO((tmp_path / "evidence" / kw["Key"]).read_bytes())})
        reviewer = RemotePortkeyReviewer(client, s3=s3, bucket="test",
            identity={"team": "platform", "principal": "operator", "request_id": "test"})
    else:
        reviewer = PortkeyReviewer(client, gateway, botocore.session.get_session())
    service = ReviewService(launch, reviewer, state.plans)
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)
    try:
        yield service, state
    finally:
        reviewer.close()
        if remote:
            gateway.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("routed", ["local", "remote"], indirect=True)
def test_native_proxy_role_selection_and_midturn_continuity(routed):
    service, state = routed
    normal = investigate(service)
    assert normal.status_code == 200
    assert normal.body["routing"]["fallback_used"] is False
    assert [c["model"] for c in normal.body["model_calls"]] == [LUNA, LUNA, TERRA, TERRA, LUNA]

    service.reviewer.gateway.set_drill(True)
    recovered = investigate(service)
    assert recovered.status_code == 200
    assert recovered.body["routing"]["fallback_used"] is True
    recovered_call = next(c for c in recovered.body["model_calls"] if c["fallback_used"])
    assert recovered_call["evidence_ready"] is True
    assert recovered_call["gateway_target"] == "config.targets[1].targets[1]"
    assert [(a["model"], a["source"], a["status_code"]) for a in recovered_call["attempts"]] == [
        (TERRA, "injected", 429), (LUNA, "bedrock", 200),
    ]
    assert [e["tool"] for e in recovered.body["events"]] == [
        "inspect_deployment", "delegate_recovery_planning",
        "get_blast_radius", "check_rollback_readiness",
    ]
    # The injected attempt never reached the upstream. The successful replacement
    # contains both completed tools, their original IDs, and their actual evidence.
    fallback = next(r["payload"] for r in state.requests if r["payload"]["model"] == LUNA and drafting(r["payload"]))
    assert fallback["instructions"] == PLANNER_PROMPT
    assert fallback["store"] is False and "previous_response_id" not in fallback
    calls = {x["call_id"] for x in fallback["input"] if x.get("type") == "function_call"}
    outputs = [x for x in fallback["input"] if x.get("type") == "function_call_output"]
    assert {x["call_id"] for x in outputs} == calls
    assert "analytics-dashboard" in json.dumps(outputs)
    assert "no restore test has been completed" in json.dumps(outputs)

    service.reviewer.gateway.set_drill(False)
    restored = investigate(service)
    assert restored.status_code == 200
    assert restored.body["routing"]["fallback_used"] is False
    assert [c["model"] for c in restored.body["model_calls"]] == [LUNA, LUNA, TERRA, TERRA, LUNA]
    assert len(list(state.plans.glob("*.md"))) == 3
    for request in state.requests:
        assert request["url"] == "https://bedrock-runtime.us-test-1.amazonaws.com/openai/v1/responses"
        assert request["headers"]["host"] == "bedrock-runtime.us-test-1.amazonaws.com"
        assert "/us-test-1/bedrock/aws4_request" in request["headers"]["authorization"]
        assert "Bearer" not in request["headers"]["authorization"]
        assert not any(k.startswith("x-portkey-") for k in request["headers"])
    assert "FAKE_TEST" not in json.dumps(recovered.body)


def test_cloud_console_evidence_requires_token_and_replay_makes_no_model_calls(routed):
    import urllib.error
    import urllib.request
    service, state = routed
    result = investigate(service)
    assert result.status_code == 200
    gateway = service.reviewer.gateway
    records = gateway.console_evidence()
    assert records and records[0]["requestOptions"][0]["module10"]["call_id"]
    assert "FAKE_TEST_" not in json.dumps(records)
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(gateway.base_url + "/__demo/console-evidence", timeout=3)  # nosec B310 - the test's local loopback gateway
    assert denied.value.code == 403
    calls_before = len(state.requests)
    gateway.console_evidence(records)
    assert len(gateway.console_evidence()) == len(records) * 2
    assert len(state.requests) == calls_before


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
def test_gateway_policy_recovers_transient_status_without_retries(routed, code):
    service, state = routed
    state.status_for = lambda p: code if p["model"] == TERRA and drafting(p) else 200
    result = investigate(service)
    assert result.status_code == 200
    call = next(c for c in result.body["model_calls"] if c["fallback_used"])
    assert [a["status_code"] for a in call["attempts"]] == [code, 200]
    assert len(state.requests) == 6
    primary, fallback = [r["payload"] for r in state.requests if drafting(r["payload"])]
    assert primary["model"] == TERRA and fallback["model"] == LUNA
    assert {k: v for k, v in primary.items() if k != "model"} == {
        k: v for k, v in fallback.items() if k != "model"
    }


@pytest.mark.parametrize("code", [400, 401, 403, 422])
def test_nonretryable_error_does_not_switch_models(routed, code):
    service, state = routed
    state.status_for = lambda p: code if p["model"] == TERRA else 200
    result = investigate(service)
    assert result.status_code == 502
    assert result.body["routing"]["fallback_attempted"] is False
    assert result.body["model_calls"][-1]["attempts"][0]["status_code"] == code
    assert len(state.requests) == 3
    assert not state.plans.exists()


def test_exhausted_fallback_returns_evidence_without_a_plan(routed):
    service, state = routed
    state.status_for = lambda p: 503 if p["instructions"] == PLANNER_PROMPT else 200
    result = investigate(service)
    assert result.status_code == 502
    assert result.body["routing"] == {
        "fallback_used": False, "fallback_attempted": True, "gateway": "portkey-local",
    }
    assert [a["status_code"] for a in result.body["model_calls"][-1]["attempts"]] == [503, 503]
    assert result.body["usage"] is None
    assert not state.plans.exists()


@pytest.mark.parametrize("status,text", [("incomplete", "Partial"), ("completed", "")])
def test_bad_agent_output_does_not_trigger_model_substitution(routed, status, text):
    service, state = routed
    state.respond = lambda p: response_payload(status=status, text=text) if drafting(p) else scripted_agent(p)
    result = investigate(service)
    assert result.status_code == 502
    assert result.body["routing"]["fallback_attempted"] is False
    assert not state.plans.exists()


@pytest.mark.parametrize("delay_body", [False, True])
def test_gateway_timeout_leaves_budget_for_backup(routed, monkeypatch, delay_body):
    service, state = routed
    original = service.reviewer.create_response

    def short_budget(**kwargs):
        if kwargs["agent"] == "Recovery Planner" and kwargs["evidence_ready"]:
            kwargs["deadline"] = time.monotonic() + 2.1
        return original(**kwargs)

    monkeypatch.setattr(service.reviewer, "create_response", short_budget)
    state.delay_for = lambda p: 0.3 if p["model"] == TERRA and drafting(p) else 0
    state.delay_body = delay_body
    result = investigate(service)
    assert result.status_code == 200
    call = next(c for c in result.body["model_calls"] if c["fallback_used"])
    assert call["attempts"][0]["source"] == "gateway_timeout"
    assert [a["status_code"] for a in call["attempts"]] == [408, 200]
    if delay_body:
        assert call["attempts"][0]["upstream_status_code"] == 200


def test_expired_request_never_invokes_gateway(routed, monkeypatch):
    service, state = routed
    monkeypatch.setattr("module10.reviewer.AgentRun", lambda: AgentRun(deadline=time.monotonic() - 1))
    result = investigate(service)
    assert result.status_code == 502
    assert not state.requests and not state.plans.exists()


def test_credentials_are_refreshed_per_turn_and_not_returned(routed, monkeypatch):
    service, state = routed
    count = 0

    def freeze():
        nonlocal count
        count += 1
        return ReadOnlyCredentials(f"ROTATING_{count}", "TEST_SECRET", f"TOKEN_{count}")

    monkeypatch.setattr(
        service.reviewer.session, "get_credentials",
        lambda: SimpleNamespace(get_frozen_credentials=freeze),
    )
    result = investigate(service)
    assert result.status_code == 200
    assert count == len(state.requests) == 5
    for i, request in enumerate(state.requests, 1):
        assert f"Credential=ROTATING_{i}/" in request["headers"]["authorization"]
        assert request["headers"]["x-amz-security-token"] == f"TOKEN_{i}"
    assert "ROTATING_" not in json.dumps(result.body)
    assert "TEST_SECRET" not in json.dumps(result.body)


def test_section_two_has_three_screens_two_requests_and_isolated_flags(routed, monkeypatch, capsys):
    from demos import module10_demo as demo

    service, state = routed
    original_flags = DEFAULT_FLAGS_PATH.read_bytes()
    runtime = RuntimeBundle(
        service, service.launch_controller, service.reviewer, TraceRecorder(), [], "local", False,
    )
    screens, results = [], []
    original = demo.DemoContext.request

    def record(context, team):
        result = original(context, team)
        results.append((len(screens), team, result.body["routing"]["fallback_used"]))
        return result

    monkeypatch.setattr(demo, "build_runtime", lambda *args, **kwargs: runtime)
    # The fixture owns cleanup so that its process lifecycle can be checked.
    monkeypatch.setattr(runtime, "close", lambda: None)
    monkeypatch.setattr(demo, "clear_screen", lambda: screens.append(True))
    monkeypatch.setattr(demo.DemoContext, "request", record)
    monkeypatch.setattr(demo.sys, "argv", ["module10_demo.py", "--section", "2", "--no-pause"])
    demo.main()
    output = capsys.readouterr().out
    assert results == [(1, "platform", False), (3, "platform", True)]
    assert len(screens) == 3
    assert "Failure drill armed" in output and "simulated 429" in output
    assert output.count("Module 10 Concept:") == 1
    assert service.reviewer.gateway.console_url in output
    assert "Logz.io" not in output and "USER" not in output
    assert "SECTION 1" not in output and "MODULE 10 — AGENT EXPOSURE" not in output
    assert "RETURN TO NORMAL OPERATION" not in output
    assert "USE THE PREFERRED MODEL AGAIN" not in output
    assert DEFAULT_FLAGS_PATH.read_bytes() == original_flags
    assert len(list(state.plans.glob("*.md"))) == 2


def test_local_console_auth_and_replayed_fallback_evidence(routed):
    service, state = routed
    gateway = service.reviewer.gateway
    token = parse_qs(urlsplit(gateway.console_url).fragment)["token"][0]
    with httpx2.Client(base_url=gateway.base_url, trust_env=False, timeout=5) as browser:
        page = browser.get("/public/logs")
        assert page.status_code == 200
        assert "Portkey AI Gateway" in page.text
        assert token not in page.text
        assert '<script src="https:' not in page.text
        assert "googletagmanager.com" not in page.text
        assert browser.get("/public/auth/session").json() == {"authenticated": False}
        assert browser.get("/log/stream").status_code == 401
        assert browser.post("/public/auth", json={"admin_token": "wrong"}).status_code == 401  # nosec B105 - a wrong token the test expects to be refused
        login = browser.post("/public/auth", json={"admin_token": token})
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert browser.get("/public/auth/session").json() == {"authenticated": True}

        assert investigate(service).status_code == 200
        gateway.set_drill(True)
        assert investigate(service).status_code == 200
        # Connecting after inference must still show both runs, including the
        # interrupted turn and its real gateway-selected replacement.
        logs = []
        with browser.stream("GET", "/log/stream") as stream:
            assert stream.status_code == 200
            lines = stream.iter_lines()
            for line in lines:
                if line.startswith("data: "):
                    entry = json.loads(line[6:])
                    if "requestOptions" in entry:
                        logs.append(entry)
                        if len(logs) == 10:
                            break
            # An already-open browser receives subsequent calls on the same
            # connection, without refreshing or replaying previous entries.
            assert investigate(service).status_code == 200
            for line in lines:
                if line.startswith("data: "):
                    entry = json.loads(line[6:])
                    if "requestOptions" in entry:
                        logs.append(entry)
                        if len(logs) == 15:
                            break
            assert len({entry["requestOptions"][0]["module10"]["call_id"] for entry in logs}) == 15
        attempts = [entry["requestOptions"][0]["module10"]["attempts"] for entry in logs]
        fallback = next(items for items in attempts if len(items) == 2)
        assert [(a["model"], a["status_code"], a["source"]) for a in fallback] == [
            (TERRA, 429, "injected"), (LUNA, 200, "bedrock"),
        ]
        text = json.dumps(logs)
        assert "FAKE_TEST" not in text
        assert "aws_secret_access_key" not in text
        assert "authorization" not in text.lower()
        assert "requestParams" in text and "function_call_output" in text
        assert token not in text


def test_interactive_demo_keeps_console_alive_until_final_enter(routed, monkeypatch, capsys):
    from demos import module10_demo as demo

    service, _ = routed
    runtime = RuntimeBundle(
        service, service.launch_controller, service.reviewer, TraceRecorder(), [], "local", False,
    )
    prompts = []

    def enter(prompt):
        prompts.append(prompt)
        assert service.reviewer.gateway.process.poll() is None
        return ""

    monkeypatch.setattr(demo, "build_runtime", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(runtime, "close", lambda: None)
    monkeypatch.setattr("builtins.input", enter)
    monkeypatch.setattr(demo.sys, "argv", ["module10_demo.py", "--section", "2"])
    demo.main()
    capsys.readouterr()
    assert len(prompts) == 4
    assert prompts[-1] == "  Press Enter to finish this section..."
