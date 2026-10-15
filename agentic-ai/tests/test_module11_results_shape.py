# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Result JSON shape (8.1 test_module11_results_shape).

Module 10's browser client (`module10/ui/details.mjs`, `app.mjs`) and its control Lambda render
the runtime's result unchanged, so every key they read must be there with Module 10's type. The
golden comparison is generated from Module 10's own `ReviewService` (its real agent loop over its
incident fixture, with a scripted model client), so a change on either side shows up here.
"""
import copy
import json
import re
import sys
import tempfile
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from module11.runtime import fake_model as F
from module11.runtime import guardrail as G
from module11.runtime import results as R
from test_module11_support import ROOT, envelope, invoke, local_dir, make_runtime, stored_result

UI_FILES = ("module10/ui/details.mjs", "module10/ui/app.mjs")
OMITTED = {"routing", "portkey_console_logs"}   # Portkey only: the UI then shows "Direct Bedrock"
PLAN_TASK = "The checkout service is throwing 502s since the 14:20 deploy. Plan the recovery."


@pytest.fixture
def d(tmp_path):
    return local_dir(tmp_path)


def _planned(d):
    doc = envelope(d)
    doc["request"]["change_summary"] = PLAN_TASK
    return doc


def _results(d):
    """A completed (with planner), a failed and a blocked result from the local runtime."""
    completed, _ = invoke(make_runtime(d, planning=True), _planned(d))

    def no_history(params):
        return F.text("Nothing to check.")
    failed_env = envelope(d)
    failed_env["request"]["request_id"] = "failed-1"
    failed, _ = invoke(make_runtime(d, model=F.FakeModelClient(no_history)), failed_env)

    class Blocking:
        def apply(self, text, source):
            return G.GuardrailResult(G.INTERVENED, "Sorry, that request was blocked.")
    blocked_env = envelope(d)
    blocked_env["request"]["request_id"] = "blocked-1"
    blocked, _ = invoke(make_runtime(d, guardrail=Blocking()), blocked_env)
    return completed, failed, blocked


# ---------------------------------------------------------------- Module 10's own response
def _module10_bodies(tmp_path, monkeypatch) -> dict[bool, dict]:
    """Module 10's ReviewService over its incident fixture, planning off and on."""
    if "openai" not in sys.modules:   # ReviewService imports OpenAIError; the tooling venv has no openai
        stub = types.ModuleType("openai")
        stub.OpenAIError = type("OpenAIError", (Exception,), {})
        monkeypatch.setitem(sys.modules, "openai", stub)
    from module10.flags import LocalLaunchDarkly
    from module10.reviewer import BedrockReviewer, ReviewService, TeamReviewRequest
    service_name = json.loads((ROOT / "module10/fixtures/incident.json").read_text())["deployment"]["service_name"]

    class Responses:
        def create(self, **params):
            offered = {t["name"] for t in params.get("tools") or []}
            history = params["input"]
            done = {i.get("name") for i in history if isinstance(i, dict) and i.get("type") == "function_call"}
            n = len(history)
            args = {"service_name": service_name}
            if "get_blast_radius" in offered:
                if not done:
                    return F.FakeResponse([F.call("get_blast_radius", args, n), F.call("check_rollback_readiness", args, n)])
                return F.text("### Finding\nThe deployment failed.\n### Recovery steps\n1. Roll back after approval.")
            if "inspect_deployment" not in done:
                return F.FakeResponse([F.call("inspect_deployment", args, n)])
            if "delegate_recovery_planning" in offered and "delegate_recovery_planning" not in done:
                return F.FakeResponse([F.call("delegate_recovery_planning", args, n)])
            return F.text("The deployment caused it. Proposal, not executed: roll back.")

    class Client:
        responses = Responses()
        base_url = "fake://module10"

        def close(self):
            pass

    class Trace:
        trace_id = "a" * 32

        @contextmanager
        def span(self, name, attributes=None):
            yield types.SimpleNamespace(set_attribute=lambda key, value: None)

    class Telemetry:
        version = "module10-test"

        @contextmanager
        def transaction(self, request):
            yield Trace()

        def release(self, trace, release, behavior):
            return {}

        def complete(self, trace, body, status):
            pass

    bodies = {}
    for planning in (False, True):
        flags = json.loads((ROOT / "module10/flags.json").read_text())
        flags["flags"]["module10-recovery-planning"]["on"] = planning   # as Module 10's control.mjs edits it
        path = tmp_path / f"flags-{planning}.json"
        path.write_text(json.dumps(flags))
        launch = LocalLaunchDarkly(path)
        try:
            service = ReviewService(launch, BedrockReviewer(Client(), "us.openai.gpt-5.6-luna"),
                                    artifact_dir=Path(tempfile.mkdtemp(dir=tmp_path)), telemetry=Telemetry())
            request = TeamReviewRequest.from_http({"change_summary": f"{service_name} failed after the deploy; plan recovery",
                                                   "request_id": f"m10-{planning}"}, "platform", "user-1")
            response = service.review(request)
        finally:
            launch.close()
        body = copy.deepcopy(response.body)
        body["artifact_id"] = body["request_id"]      # module10/cloud_app.py adds it before writing
        if body.get("recovery_plan"):
            body["recovery_plan"].pop("path", None)   # and drops the local path
        bodies[planning] = body
    assert bodies[True]["recovery_plan"] and bodies[True]["capabilities"]["recovery_planning"] is True
    return bodies


def _kind(value):
    return {dict: "object", list: "array", str: "string", bool: "boolean", int: "number", float: "number",
            type(None): "null"}[type(value)]


def test_golden_shape_matches_module10s_own_response(d, tmp_path, monkeypatch):
    golden = _module10_bodies(tmp_path, monkeypatch)
    ours = {True: invoke(make_runtime(d, planning=True), _planned(d))[0]}
    plain = envelope(d)
    plain["request"]["request_id"] = "plain-1"
    ours[False] = invoke(make_runtime(d, planning=False), plain)[0]
    for planning in (False, True):
        m10, m11 = golden[planning], ours[planning]
        assert m10["status"] == m11["status"] == "completed"
        missing = set(m10) - OMITTED - set(m11)
        assert not missing, f"keys Module 10 returns that the m11 result lacks: {sorted(missing)}"
        for key in set(m10) - OMITTED:
            if m10[key] is not None and m11[key] is not None:
                assert _kind(m10[key]) == _kind(m11[key]), key
        assert set(m11) - set(m10) <= {"m11"} | ({"recovery_plan"} if planning is False else set())
        for key in ("behavior", "capabilities", "model", "usage", "release"):
            assert set(m10[key]) <= set(m11[key]), key
        if planning:
            assert set(m10["recovery_plan"]) == set(m11["recovery_plan"])
        event_keys = set().union(*(set(e) for e in m10["events"]))
        assert all(event_keys <= set(e) for e in m11["events"] if e["status"] == "completed")
        call_keys = set().union(*(set(c) for c in m10["model_calls"]))
        assert all(call_keys <= set(c) for c in m11["model_calls"])
        assert m11["review"].endswith(R.PLANNED if planning else R.NOT_PLANNED)
        assert m10["review"].endswith(R.PLANNED if planning else R.NOT_PLANNED)


def test_every_key_the_module10_ui_reads_is_present(d):
    completed, failed, blocked = _results(d)
    read = set()
    for rel in UI_FILES:
        read |= set(re.findall(r"\bbody\.(\w+)", (ROOT / rel).read_text(encoding="utf-8")))
    assert {"request_id", "team", "status", "capabilities", "release", "events", "model_calls", "usage",
            "trace_id", "review", "error", "recovery_plan", "artifact_id"} <= read
    present = set(completed) | set(failed) | set(blocked)
    assert read - OMITTED <= present, sorted(read - OMITTED - present)
    assert not (OMITTED & present)
    for body in (completed, failed, blocked):
        assert re.fullmatch(r"[a-f0-9]{32}", body["trace_id"])            # app.mjs links only this shape
        assert isinstance(body["events"], list) and isinstance(body["model_calls"], list)
        assert body["artifact_id"] == body["request_id"]
        assert body["capabilities"]["investigation"] is True
        assert isinstance(body["capabilities"]["recovery_planning"], bool)
        for call in body["model_calls"]:
            assert {"agent", "requested_model", "model", "attempts"} <= set(call)
        for event in body["events"]:
            assert {"agent", "tool", "status"} <= set(event)
    release = completed["release"]
    assert {"flag_key", "version", "reason"} <= set(release) and "kind" in release["reason"]
    assert set(completed["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}
    assert completed["recovery_plan"]["text"] and completed["recovery_plan"]["changes_executed"] is False
    assert any(e["agent"] == "Recovery Planner" for e in completed["events"])   # "Planner executed: Yes"


def test_status_specific_fields(d):
    completed, failed, blocked = _results(d)
    assert completed["status"] == "completed" and "error" not in completed and completed["review"]
    assert failed["status"] == "failed" and failed["error"] and failed["detail"]
    assert "This investigation is incomplete" in failed["review"]                 # it still answers
    assert failed["recovery_plan"] is None
    assert blocked["status"] == "blocked" and blocked["review"] == blocked["error"] == "Sorry, that request was blocked."
    assert blocked["m11"]["guardrail"] == {"action": G.INTERVENED, "source": "INPUT"}
    assert blocked["model_calls"] == []                                          # blocked before any model call


def test_m11_key_and_the_stored_copy(d):
    completed, failed, _ = _results(d)
    for body in (completed, failed):
        m11 = body["m11"]
        assert {"manifestVersion", "behaviorDigest", "release", "runtimeDigest", "bundleVersion", "bundleOverride",
                "variant", "conversationId", "runtimeSessionId", "flagsHash", "toolsOffered", "cachedInputTokens",
                "guardrail"} <= set(m11)
        assert body["service_version"] == m11["runtimeDigest"]
        assert body["evidence_source"] == R.EVIDENCE_SOURCE
        assert stored_result(d, body) == json.loads(json.dumps(body, sort_keys=True, default=str))
    assert completed["m11"]["cachedInputTokens"] > 0
    assert R.result_key("platform", "user-1", "r1") == "results/platform/user-1/r1.json"
