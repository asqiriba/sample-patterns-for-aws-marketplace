# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Side-effect suppression in shadow sessions, side-effect stamps, and the handlers' data_mismatch
check (8.1 test_module11_tools_shadow)."""
import json

import pytest

from module11.aws import objects
from module11.runtime import fake_model as F
from module11.runtime.config import load_component
from module11.runtime.telemetry import RecordingBackend
from module11.runtime.tools import CallContext, LocalInvoker, ToolClient
from module11.release import manifest as M
from test_module11_support import ROOT, config_doc, envelope, invoke, local_dir, make_runtime

NOTE = "record_incident_note"
STAMP_KEYS = {"manifestVersion", "behaviorDigest", "variant", "sessionId", "conversationId", "requestId", "stage",
              "release"}
EVIDENCE_TOOLS = ["get_deployment_history", "query_metrics", "get_service_logs", "get_blast_radius",
                  "check_rollback_readiness"]


@pytest.fixture
def d(tmp_path):
    return local_dir(tmp_path)


class CountingInvoker:
    def __init__(self):
        self.arns = []
        self.local = LocalInvoker()

    def invoke(self, arn, event, timeout):
        self.arns.append(arn)
        return self.local.invoke(arn, event, timeout)


def _side_effects(d):
    return sorted((d / "bucket" / "side-effects").rglob("*.json")) if (d / "bucket" / "side-effects").is_dir() else []


def _memory(d):
    path = d / "memory" / "records.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


# ---------------------------------------------------------------- shadow suppression (N15)
def test_shadow_session_suppresses_the_note_everywhere(d):
    backend, invoker = RecordingBackend(), CountingInvoker()
    runtime = make_runtime(d, backend=backend, invoker=invoker)
    body, streamed = invoke(runtime, envelope(d, "envelope-shadow.json"))
    assert body["status"] == "completed" and body["m11"]["variant"] == "shadow"
    # No Lambda call, no side-effect object, no Memory record.
    assert f"local:{NOTE}" not in invoker.arns and len(invoker.arns) >= 4
    assert _side_effects(d) == [] and _memory(d) == []
    # The span attribute.
    note_spans = [s for s in backend.spans if s.attributes.get("gen_ai.tool.name") == NOTE]
    assert len(note_spans) == 1 and note_spans[0].attributes["m11.shadow_suppressed"] is True
    assert all(s.attributes["m11.shadow"] is True for s in backend.spans if s.trace_id == body["trace_id"])
    result = json.loads(note_spans[0].attributes["gen_ai.tool.call.result"])
    shadow_result = load_component(M.bundle_component(ROOT), "v").tools[NOTE].shadow_result
    assert {k: result[k] for k in shadow_result} == shadow_result
    assert result["note_id"].startswith("shadow-") and result["shadow_suppressed"] is True
    # The streamed tool event and the result's events[] entry.
    streamed_note = [data for name, data in streamed if name == "tool" and data["tool"] == NOTE]
    assert streamed_note == [{"agent": "DevOps Companion", "tool": NOTE, "status": "completed",
                              "summary": shadow_result["summary"], "shadow_suppressed": True}]
    entry = next(e for e in body["events"] if e["tool"] == NOTE)
    assert entry["shadow_suppressed"] is True
    assert all("shadow_suppressed" not in e for e in body["events"] if e["tool"] != NOTE)


def test_non_shadow_session_writes_a_stamped_note_and_memory_record(d):
    backend, invoker = RecordingBackend(), CountingInvoker()
    doc = envelope(d)
    body, streamed = invoke(make_runtime(d, backend=backend, invoker=invoker), doc)
    assert f"local:{NOTE}" in invoker.arns
    [path] = _side_effects(d)
    note = json.loads(path.read_text())
    stamp = note["stamp"]
    assert set(stamp) == STAMP_KEYS
    ctx = doc["context"]
    assert stamp == {"manifestVersion": ctx["manifestVersion"], "behaviorDigest": ctx["behaviorDigest"],
                     "variant": ctx["variant"], "sessionId": body["m11"]["runtimeSessionId"],
                     "conversationId": body["m11"]["conversationId"], "requestId": doc["request"]["request_id"],
                     "stage": "local", "release": ctx["release"]}
    rel = path.relative_to(d / "bucket").as_posix()
    assert rel.startswith(f"side-effects/local/{ctx['release']}/{stamp['sessionId']}/")
    [record] = _memory(d)
    assert record["namespaces"] == [f"/devops-companion/local/release/{ctx['release']}/"]
    assert record["metadata"]["manifestVersion"] == ctx["manifestVersion"]
    assert record["metadata"]["sessionId"] == stamp["sessionId"]
    span = next(s for s in backend.spans if s.attributes.get("gen_ai.tool.name") == NOTE)
    assert "m11.shadow_suppressed" not in span.attributes
    assert not any(data.get("shadow_suppressed") for name, data in streamed if name == "tool")


def test_tool_client_never_invokes_a_mutating_tool_in_shadow():
    bundle = load_component(M.bundle_component(ROOT), "v")

    class Refusing:
        def invoke(self, arn, event, timeout):
            raise AssertionError("no Lambda call in a shadow session")
    client = ToolClient(bundle.tools, {NOTE: "arn:aws:lambda:us-east-1:111122223333:function:x:1"}, Refusing())
    ctx = CallContext(stage="prod", manifest_version="2026-10-20T14:02:11Z/abc1234", behavior_digest="sha256:" + "a" * 64,
                      release="harness-v8", variant="shadow", session_id="s" * 40, conversation_id="conv-12345",
                      request_id="r1", team="platform", shadow=True, data={})
    outcome = client.call(NOTE, {"service_name": "checkout-service", "probable_cause": "x", "log_line": "y"}, ctx)
    assert outcome.ok and outcome.shadow_suppressed and outcome.result["recorded"] is False
    assert client.calls == []


def test_handler_refuses_a_shadow_call_context(d):
    data = config_doc(d)
    event = {"arguments": {"service_name": "checkout-service", "probable_cause": "x", "log_line": "y"},
             "context": {"stage": "local", "manifestVersion": "2026-10-20T14:02:11Z/abc1234",
                         "behaviorDigest": "sha256:" + "a" * 64, "variant": "shadow", "sessionId": "s1",
                         "conversationId": "c1", "requestId": "r1", "team": "platform", "release": "local",
                         "shadow": True, "sequence": 1,
                         "data": {k: data[k] for k in ("snapshot", "evidence", "memoryId", "artifactsBucket")}}}
    out = LocalInvoker().invoke(f"local:{NOTE}", event, 10)
    assert out["ok"] is False and out["error"]["code"] == "invalid_arguments"
    assert _side_effects(d) == []


# ---------------------------------------------------------------- data_mismatch (M6)
def _event(d, tool, arguments=None):
    data = config_doc(d)
    return {"arguments": arguments or {"service_name": "checkout-service"},
            "context": {"stage": "local", "release": "local", "shadow": False, "sequence": 1,
                        "data": {k: data[k] for k in ("snapshot", "evidence", "memoryId", "artifactsBucket")}}}


@pytest.mark.parametrize("tool", EVIDENCE_TOOLS)
def test_evidence_handlers_answer_and_detect_a_mismatched_object(d, tool, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", None)
    invoker = LocalInvoker()
    assert invoker.invoke(f"local:{tool}", _event(d, tool), 10)["ok"] is True
    meta = d / "bucket" / "evidence" / config_doc(d)["evidence"]["sha256"][7:] / "incidents" / "checkout-service.json.metadata.json"
    meta.write_text(json.dumps({objects.SHA_KEY: "sha256:" + "0" * 64}))
    out = invoker.invoke(f"local:{tool}", _event(d, tool), 10)
    assert out == {"ok": False, "error": {"code": "data_mismatch", "message": out["error"]["message"]}}
    meta.unlink()   # missing metadata is a mismatch too
    assert invoker.invoke(f"local:{tool}", _event(d, tool), 10)["error"]["code"] == "data_mismatch"


@pytest.mark.parametrize("tool", EVIDENCE_TOOLS)
def test_evidence_handlers_report_unknown_services(d, tool):
    out = LocalInvoker().invoke(f"local:{tool}", _event(d, tool, {"service_name": "unknown-service"}), 10)
    assert out["ok"] is False and out["error"]["code"] == "not_found"


def test_search_runbooks_detects_a_mismatched_snapshot(d):
    invoker = LocalInvoker()
    args = {"query": "checkout-service 502 after deploy connection pool"}
    ok = invoker.invoke("local:search_runbooks", _event(d, "search_runbooks", args), 10)
    assert ok["ok"] is True and ok["result"]["results"][0]["docId"] == "runbook-checkout-502"
    event = _event(d, "search_runbooks", args)
    event["context"]["data"]["snapshot"]["sha256"] = "sha256:" + "1" * 64   # another arm's pin
    out = invoker.invoke("local:search_runbooks", event, 10)
    assert out["ok"] is False and out["error"]["code"] == "data_mismatch"


def test_runtime_surfaces_data_mismatch_as_a_failed_tool_event(d):
    ref = config_doc(d)["evidence"]["sha256"][7:]
    for meta in (d / "bucket" / "evidence" / ref / "incidents").glob("*.metadata.json"):
        meta.write_text(json.dumps({objects.SHA_KEY: "sha256:" + "0" * 64}))
    body, _ = invoke(make_runtime(d, model=F.FakeModelClient()), envelope(d))
    first = body["events"][0]
    assert (first["tool"], first["status"], first["error"]) == ("get_deployment_history", "failed", "data_mismatch")
    assert body["status"] == "failed"   # the required tool never completed
