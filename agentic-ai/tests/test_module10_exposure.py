"""Focused tests for the Module 10 safe-exposure demo."""

from __future__ import annotations

import json
from pathlib import Path
import re
import urllib.request

import pytest

from module10.control_planes import (
    IntegrationUnavailable,
    MockLaunchDarkly,
    MockPortkeyGateway,
)
from module10.exposure import ExposureService
from module10.models import ReviewRequest, RuntimeStatus
from module10.runtime import build_runtime
from module10.server import create_server, start_server
from module10.telemetry import TraceRecorder


class AlwaysFailingGateway:
    def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        raise RuntimeError("simulated gateway outage")


@pytest.fixture
def service() -> ExposureService:
    return ExposureService(
        MockLaunchDarkly(),
        MockPortkeyGateway(),
        TraceRecorder(),
    )


def test_stable_tenant_uses_stable_release_and_primary_model(
    service: ExposureService,
) -> None:
    response = service.review(
        ReviewRequest(
            tenant_id="platform-prod",
            change_summary="Add encryption to the reporting service.",
        )
    )

    assert response.status_code == 200
    assert response.body["release"]["variation"] == "stable"
    assert response.body["release"]["agent_version"] == "reviewer-v1"
    assert response.body["route"]["degraded"] is False
    assert response.body["route"]["fallback_used"] is False
    assert response.body["route"]["attempts"][0]["status_code"] == 200


def test_candidate_request_recovers_from_a_primary_throttle(
    service: ExposureService,
) -> None:
    gateway = service.model_gateway
    assert isinstance(gateway, MockPortkeyGateway)

    first = service.review(
        ReviewRequest(
            tenant_id="platform-beta",
            change_summary="Deploy the new reporting service.",
        )
    )
    gateway.arm_primary_throttle("platform-beta")
    second = service.review(
        ReviewRequest(
            tenant_id="platform-beta",
            change_summary="Deploy the new reporting service.",
        )
    )

    assert first.body["release"]["variation"] == "candidate"
    assert first.body["route"]["fallback_used"] is False
    assert second.status_code == 200
    assert second.body["route"]["degraded"] is True
    assert second.body["route"]["fallback_used"] is True
    assert [attempt["status_code"] for attempt in second.body["route"]["attempts"]] == [
        429,
        200,
    ]
    assert second.body["route"]["final_target"] == "portkey-fallback"


def test_kill_switch_returns_structured_503_without_gateway_call(
    service: ExposureService,
) -> None:
    launch = service.launch_controller
    gateway = service.model_gateway
    assert isinstance(launch, MockLaunchDarkly)
    assert isinstance(gateway, MockPortkeyGateway)

    launch.set_agent_enabled(False)
    response = service.review(
        ReviewRequest(
            tenant_id="platform-beta",
            change_summary="Attempt a disabled release.",
        )
    )

    assert response.status_code == 503
    assert response.body["status"] == "disabled"
    assert response.body["release"]["variation"] == "disabled"
    assert gateway.call_count("platform-beta") == 0


def test_gateway_exhaustion_returns_structured_degraded_503() -> None:
    service = ExposureService(
        MockLaunchDarkly(),
        AlwaysFailingGateway(),
        TraceRecorder(),
    )

    response = service.review(
        ReviewRequest(
            tenant_id="platform-beta",
            change_summary="The gateway is unavailable.",
        )
    )

    assert response.status_code == 503
    assert response.body["status"] == "degraded"
    assert response.body["route"]["degraded"] is True
    trace = service.telemetry.get(response.body["trace_id"])
    assert trace is not None
    gateway_span = next(
        span for span in trace["spans"] if span["name"] == "portkey.gateway"
    )
    assert gateway_span["status"] == "error"
    assert gateway_span["events"][0]["name"] == "error"


def test_trace_contains_release_route_and_safe_attributes(
    service: ExposureService,
) -> None:
    gateway = service.model_gateway
    assert isinstance(gateway, MockPortkeyGateway)
    gateway.arm_primary_throttle("platform-beta")

    response = service.review(
        ReviewRequest(
            tenant_id="platform-beta",
            change_summary="Do not put secret=top-secret in telemetry.",
        )
    )
    trace = service.telemetry.get(response.body["trace_id"])

    assert trace is not None
    assert [span["name"] for span in trace["spans"]] == [
        "POST /invoke",
        "launchdarkly.evaluate",
        "agent.prepare",
        "portkey.gateway",
        "response.serialize",
    ]
    gateway_span = next(
        span for span in trace["spans"] if span["name"] == "portkey.gateway"
    )
    assert gateway_span["attributes"]["ai.gateway.fallback"] is True
    assert gateway_span["attributes"]["ai.gateway.primary_status_code"] == 429
    assert "top-secret" not in json.dumps(trace)
    assert "Do not put" not in json.dumps(trace)


def test_http_endpoint_round_trip() -> None:
    service = ExposureService(
        MockLaunchDarkly(),
        MockPortkeyGateway(),
        TraceRecorder(),
    )
    server, _thread = start_server(
        service,
        runtime_mode="mock",
        runtime_statuses=[
            RuntimeStatus("LaunchDarkly", "mock", "test"),
        ],
    )
    host, port = server.server_address
    request = urllib.request.Request(
        f"http://{host}:{port}/invoke",
        data=json.dumps(
            {
                "tenant_id": "platform-prod",
                "change_summary": "Run the endpoint contract test.",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # nosec B310 - the test's own loopback server
            body = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert body["status"] == "completed"
        assert body["route"]["attempts"][0]["status_code"] == 200

        with urllib.request.urlopen(  # nosec B310 - the test's own loopback server
            f"http://{host}:{port}/status",
            timeout=5,
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        assert status["mode"] == "mock"
        assert status["components"][0]["component"] == "LaunchDarkly"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", ["mock", "local", "live"])
def test_unauthenticated_endpoint_binds_only_to_loopback(mode: str) -> None:
    with pytest.raises(ValueError, match="must bind to localhost"):
        create_server(service=object(), host="0.0.0.0", port=0, runtime_mode=mode)  # nosec B104 - asserts the bind rule; nothing binds

    server = create_server(service=object(), host="127.0.0.1", port=0, runtime_mode=mode)
    server.server_close()


def test_runtime_defaults_to_mock_without_external_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULE10_MODE", raising=False)
    monkeypatch.delenv("AGENT_MOCK_MODE", raising=False)

    runtime = build_runtime()

    assert runtime.mode == "mock"
    assert runtime.live_requested is False
    assert all(status.mode == "mock" for status in runtime.statuses[:2])


def test_local_mode_fails_fast_without_a_flag_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODULE10_LD_FILE", str(tmp_path / "missing.json"))

    with pytest.raises(IntegrationUnavailable, match="flag file not found"):
        build_runtime("local")


def test_live_mode_does_not_fall_back_to_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "LD_SDK_KEY",
        "LAUNCHDARKLY_SDK_KEY",
        "PORTKEY_API_KEY",
        "PORTKEY_CONFIG_ID",
        "PORTKEY_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(IntegrationUnavailable):
        build_runtime("live")


def test_module10_has_no_environment_specific_aws_defaults() -> None:
    roots = [
        Path("module10"),
        Path("demos/module10_demo.py"),
    ]
    source = "\n".join(
        path.read_text(encoding="utf-8")
        if path.is_file()
        else "\n".join(
            child.read_text(encoding="utf-8")
            for child in path.rglob("*.py")
            if not any(part.startswith(".") for part in child.parts)
        )
        for path in roots
    )

    assert not re.search(r"(?<!\d)\d{12}(?!\d)", source)
    assert not re.search(r"\b(?:us|eu|ap|ca|sa|me|af|il|mx)-(?:gov-)?[a-z]+-\d\b", source)
    assert "amazon.nova" not in source.lower()
    assert "profile_name=" not in source
    assert "/agent/review" not in source
