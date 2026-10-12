"""Actual SDK tool loops and local flag evaluation, without paid inference."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import time
import urllib.error
import urllib.request

import httpx2
import openai
import pytest

from module10.control_planes import IntegrationUnavailable
from module10.flags import (
    DEFAULT_FLAGS_PATH, LocalLaunchDarkly, set_recovery_planning_enabled,
)
from module10.recovery import COMPANION_PROMPT, PLANNER_PROMPT, RecoveryEvidence
from module10.reviewer import BedrockReviewer, ReviewService, TeamReviewRequest
from module10.runtime import build_runtime
from module10.server import start_server

TASK = "Investigate the failed reporting-service deployment and prepare a recovery plan."
PLAN = (
    "### Finding\nThe schema migration affects report-exporter and billing-reader.\n"
    "### Recovery steps\nValidate compatibility and test the backup restore before recovery.\n"
    "### Approval needed\nThe release owner must approve. No changes executed."
)


def wait_for_planning(launch, enabled):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if launch.evaluate("platform")["recovery_planning_enabled"] is enabled:
            return
        time.sleep(0.05)
    pytest.fail(f"SDK did not reload planning={enabled}")


@pytest.fixture
def launch(tmp_path):
    path = tmp_path / "flags.json"
    path.write_bytes(DEFAULT_FLAGS_PATH.read_bytes())
    controller = LocalLaunchDarkly(path)
    try:
        yield controller
    finally:
        controller.close()


def function_call(name, *, arguments=None):
    return {
        "type": "function_call", "id": "item-" + name, "call_id": "call-" + name,
        "name": name, "arguments": arguments or json.dumps({"service_name": "reporting-service"}),
        "status": "completed",
    }


def response_payload(*, status="completed", text="Investigation complete.", calls=None):
    return {
        "id": "resp-demo", "object": "response", "created_at": 1, "status": status,
        "model": "us.openai.gpt-5.6-luna",
        "output": calls if calls is not None else [{
            "type": "message", "id": "msg-demo", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
    }


def scripted_agent(payload):
    """Supply model tool requests; production code executes every handler."""
    completed = {item.get("name") for item in payload["input"] if item.get("type") == "function_call"}
    tools = {tool["name"] for tool in payload["tools"]}
    if payload["instructions"] == PLANNER_PROMPT:
        if not completed:
            return response_payload(calls=[
                function_call("get_blast_radius"), function_call("check_rollback_readiness"),
            ])
        return response_payload(text=PLAN)
    if "inspect_deployment" not in completed:
        return response_payload(calls=[function_call("inspect_deployment")])
    if "delegate_recovery_planning" in tools and "delegate_recovery_planning" not in completed:
        return response_payload(calls=[function_call("delegate_recovery_planning")])
    return response_payload(text="A schema mismatch caused the failed smoke test.")


@pytest.fixture
def responses_client(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_REGION", "us-test-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("MODULE10_ARTIFACT_DIR", str(tmp_path / "plans"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("MODULE10_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.setenv("AWS_BEDROCK_BASE_URL", "https://invalid.example/openai/v1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "unused")
    state = SimpleNamespace(
        requests=[], respond=scripted_agent, status_code=200, hook=None, plans=tmp_path / "plans",
    )

    def handle(request):
        state.requests.append(request)
        payload = json.loads(request.content)
        if state.hook:
            state.hook(payload)
        return httpx2.Response(state.status_code, json=state.respond(payload))

    original_client = openai.OpenAI
    monkeypatch.setattr(
        openai, "OpenAI",
        lambda **kwargs: original_client(
            **kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(handle))
        ),
    )
    reviewer = BedrockReviewer.from_env()
    try:
        yield reviewer, state
    finally:
        reviewer.close()


def investigate(service, team="platform", request_id="test"):
    return service.review(TeamReviewRequest(team, TASK, request_id))


def test_flag_targets_capability_and_reloads_withdrawal(launch):
    assert launch.evaluate("platform")["recovery_planning_enabled"] is False
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)
    assert launch.evaluate("platform")["reason"]["kind"] == "TARGET_MATCH"
    assert launch.evaluate("payments")["recovery_planning_enabled"] is False
    assert launch.evaluate("new-team")["recovery_planning_enabled"] is False
    set_recovery_planning_enabled(launch.path, False)
    wait_for_planning(launch, False)
    assert launch.evaluate("platform")["reason"]["kind"] == "OFF"


def test_invalid_flag_file_fails_startup(tmp_path):
    path = tmp_path / "flags.json"
    path.write_text('{"flags": {}}')
    with pytest.raises(IntegrationUnavailable, match="Cannot load"):
        LocalLaunchDarkly(path)


def test_capability_lifecycle_and_real_handoff(launch, responses_client):
    reviewer, state = responses_client
    service = ReviewService(launch, reviewer)
    before = investigate(service)
    assert before.status_code == 200
    assert before.body["recovery_plan"] is None
    assert before.body["review"].endswith(
        "Investigation complete. No recovery plan generated; planning is handed to the operator."
    )
    assert [e["tool"] for e in before.body["events"]] == ["inspect_deployment"]

    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)
    enabled = investigate(service)
    assert enabled.status_code == 200
    assert [(e["agent"], e["tool"]) for e in enabled.body["events"]] == [
        ("DevOps Companion", "inspect_deployment"),
        ("DevOps Companion", "delegate_recovery_planning"),
        ("Recovery Planner", "get_blast_radius"),
        ("Recovery Planner", "check_rollback_readiness"),
    ]
    assert all(e["status"] == "completed" for e in enabled.body["events"])
    assert enabled.body["usage"]["total_tokens"] == 5 * 160
    plan = enabled.body["recovery_plan"]
    assert enabled.body["review"].endswith("Recovery plan created. Awaiting operator review.")
    assert plan["changes_executed"] is False
    assert PLAN in Path(plan["path"]).read_text()
    assert "No changes executed" in Path(plan["path"]).read_text()

    set_recovery_planning_enabled(launch.path, False)
    wait_for_planning(launch, False)
    after = investigate(service)
    assert after.status_code == 200
    assert after.body["recovery_plan"] is None
    assert after.body["review"].endswith(
        "Investigation complete. No recovery plan generated; planning is handed to the operator."
    )
    assert [e["tool"] for e in after.body["events"]] == ["inspect_deployment"]
    assert len(list(state.plans.glob("*.md"))) == 1
    assert len(state.requests) == 9


def test_runtime_responses_sigv4_and_tool_history(launch, responses_client):
    reviewer, state = responses_client
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 200
    for request in state.requests:
        payload = json.loads(request.content)
        assert payload["model"] == "us.openai.gpt-5.6-luna"
        assert payload["instructions"] == COMPANION_PROMPT
        assert payload["store"] is False
        assert "previous_response_id" not in payload
        assert {t["name"] for t in payload["tools"]} == {"inspect_deployment"}
        assert str(request.url) == "https://bedrock-runtime.us-test-1.amazonaws.com/openai/v1/responses"
        assert "/us-test-1/bedrock/aws4_request" in request.headers["Authorization"]
    history = json.loads(state.requests[1].content)["input"]
    call = next(item for item in history if item.get("type") == "function_call")
    output = next(item for item in history if item.get("type") == "function_call_output")
    assert output["call_id"] == call["call_id"]
    assert "report_owner" in output["output"]


def test_withdrawal_affects_new_requests_not_an_admitted_planner(launch, responses_client):
    reviewer, state = responses_client
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)

    def withdraw(_):
        if len(state.requests) == 1:
            set_recovery_planning_enabled(launch.path, False)
            wait_for_planning(launch, False)

    state.hook = withdraw
    service = ReviewService(launch, reviewer)
    first = investigate(service)
    second = investigate(service)
    assert first.body["recovery_plan"] is not None
    assert second.body["recovery_plan"] is None


def test_unadvertised_planner_call_is_blocked_by_application(launch, responses_client):
    reviewer, state = responses_client
    state.respond = lambda _: response_payload(calls=[function_call("delegate_recovery_planning")])
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert "unavailable capability" in result.body["detail"]
    assert result.body["events"][0]["status"] == "failed"
    assert len(state.requests) == 1
    assert not state.plans.exists()


def test_planner_cannot_handoff_before_inspection(launch, responses_client):
    reviewer, state = responses_client
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)
    state.respond = lambda _: response_payload(calls=[function_call("delegate_recovery_planning")])
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert "Inspect the deployment" in result.body["detail"]
    assert len(state.requests) == 1


@pytest.mark.parametrize("arguments", ["not-json", "[]", '{"service_name": 123}', '{"service_name": "other-service"}'])
def test_bad_tool_arguments_do_not_fabricate_evidence(launch, responses_client, arguments):
    reviewer, state = responses_client
    state.respond = lambda _: response_payload(calls=[function_call("inspect_deployment", arguments=arguments)])
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert not state.plans.exists()


@pytest.mark.parametrize("status,text", [("incomplete", "Truncated"), ("completed", ""), ("completed", "Unverified diagnosis.")])
def test_incomplete_empty_or_unverified_output_fails(launch, responses_client, status, text):
    reviewer, state = responses_client
    state.respond = lambda _: response_payload(status=status, text=text)
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert result.body["status"] == "failed"
    assert "review" not in result.body
    assert not state.plans.exists()


def test_planner_must_gather_evidence_before_saving_plan(launch, responses_client):
    reviewer, state = responses_client
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)
    state.respond = lambda p: (
        response_payload(text=PLAN) if p["instructions"] == PLANNER_PROMPT else scripted_agent(p)
    )
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert "required evidence checks" in result.body["detail"]
    assert not state.plans.exists()


def test_failed_companion_does_not_publish_a_partial_run_plan(launch, responses_client):
    reviewer, state = responses_client
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)

    def fail_after_handoff(payload):
        if payload["instructions"] == COMPANION_PROMPT and any(
            item.get("name") == "delegate_recovery_planning" for item in payload["input"]
        ):
            return response_payload(status="incomplete", text="Partial summary.")
        return scripted_agent(payload)

    state.respond = fail_after_handoff
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert len(result.body["model_calls"]) == 5
    assert not state.plans.exists()


def test_repeated_tool_calls_are_bounded(launch, responses_client):
    reviewer, state = responses_client
    state.respond = lambda _: response_payload(calls=[function_call("inspect_deployment")])
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert "repeat" in result.body["detail"]
    assert len(state.requests) == 2


def test_upstream_error_has_no_mock_fallback(launch, responses_client):
    reviewer, state = responses_client
    state.status_code = 429
    state.respond = lambda _: {"error": {"message": "Rate exceeded", "type": "throttling"}}
    result = investigate(ReviewService(launch, reviewer))
    assert result.status_code == 502
    assert "Rate exceeded" in result.body["detail"]
    assert len(state.requests) == 1


def test_dependency_tool_follows_consumers_and_preserves_missing_checks():
    evidence = RecoveryEvidence()
    result = evidence.get_blast_radius("reporting-service")
    assert result["affected_services"] == ["billing-reader", "report-exporter", "analytics-dashboard"]
    readiness = evidence.check_rollback_readiness("reporting-service")
    assert readiness["application_only_rollback_safe"] is False
    assert readiness["execution_authorized"] is False
    assert "no restore test" in readiness["backup"]


def test_http_contract_and_other_team_cannot_enable_planner(launch, responses_client):
    reviewer, state = responses_client
    server, thread = start_server(ReviewService(launch, reviewer), runtime_mode="local")
    url = f"http://127.0.0.1:{server.server_port}"

    def post(body, team=None):
        headers = {"Content-Type": "application/json"}
        if team is not None:
            headers["X-Demo-Team"] = team
        request = urllib.request.Request(url + "/invoke", data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310 - the test's own loopback server
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        for body, team in [
            ({"change_summary": TASK}, None),
            ({"change_summary": TASK}, " "),
            ({"change_summary": 123}, "platform"),
            ({"change_summary": ""}, "platform"),
            ({"change_summary": "x" * 4001}, "platform"),
            ({"change_summary": TASK, "recovery_planning_enabled": True}, "payments"),
            ({"change_summary": TASK, "tenant_id": "platform"}, "payments"),
            ([], "platform"),
        ]:
            assert post(body, team)[0] == 400
        assert not state.requests
        set_recovery_planning_enabled(launch.path, True)
        wait_for_planning(launch, True)
        code, body = post({"change_summary": TASK}, "payments")
        assert code == 200
        assert body["recovery_plan"] is None
        assert body["capabilities"]["recovery_planning"] is False
        with urllib.request.urlopen(url + "/status", timeout=5) as response:  # nosec B310 - the test's own loopback server
            status = json.load(response)
        assert status["teams"]["platform"]["recovery_planning_enabled"] is True
        assert len(state.requests) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_caller_request_id_does_not_control_artifact_path(launch, responses_client):
    reviewer, state = responses_client
    set_recovery_planning_enabled(launch.path, True)
    wait_for_planning(launch, True)
    result = investigate(ReviewService(launch, reviewer), request_id="../../escape")
    path = Path(result.body["recovery_plan"]["path"])
    assert path.parent == state.plans.resolve()
    assert path.name.startswith("recovery-plan-")


def test_local_runtime_needs_no_gateway_configuration(monkeypatch, launch, responses_client):
    reviewer, _ = responses_client
    monkeypatch.setattr(BedrockReviewer, "from_env", lambda: reviewer)
    for name in ("PORTKEY_CONFIG_ID", "PORTKEY_API_KEY", "LD_SDK_KEY"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime("local", flags_path=launch.path)
    try:
        assert isinstance(runtime.service, ReviewService)
        assert [status.component for status in runtime.statuses] == ["LaunchDarkly", "Amazon Bedrock"]
    finally:
        runtime.close()


def test_walkthrough_ends_after_nonpilot_team_without_withdrawal(monkeypatch, responses_client, capsys):
    from demos import module10_demo as demo

    _, state = responses_client
    screens, results = [], []
    original = demo.DemoContext.request

    def record(context, team):
        response = original(context, team)
        results.append((len(screens), team, response.body["recovery_plan"] is not None))
        if team == "payments":
            assert context.runtime.launch_controller.evaluate("platform")["recovery_planning_enabled"] is True
            assert [event["tool"] for event in response.body["events"]] == ["inspect_deployment"]
            assert response.body["review"].endswith(
                "Investigation complete. No recovery plan generated; planning is handed to the operator."
            )
        return response

    monkeypatch.setattr(demo.sys, "argv", [
        "module10_demo.py", "--section", "1", "--mode", "local", "--no-pause",
    ])
    monkeypatch.setattr(demo, "clear_screen", lambda: screens.append(True))
    monkeypatch.setattr(demo.DemoContext, "request", record)
    demo.main()
    output = capsys.readouterr().out
    assert results == [
        (2, "platform", False), (4, "platform", True),
        (4, "payments", False),
    ]
    assert len(screens) == 4  # Setup, investigation, flag change, planner + nonpilot.
    assert len(state.requests) == 9
    assert "WITHDRAW PLANNING" not in output
    assert "after withdrawal" not in output
    assert "Saved plan: ./artifacts/recovery-plan-" in output
    assert str(state.plans) not in output
    assert output.count("Module 10 Concept:") == 1
    assert "Portkey" not in output and "Logz.io" not in output
