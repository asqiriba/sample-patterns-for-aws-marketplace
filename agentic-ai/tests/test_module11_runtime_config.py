# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Runtime config, bundle component, identity stamping, baggage override, binding, M13 imports
and sampling (8.1 test_module11_runtime_config)."""
import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from module11.release import manifest as M
from module11.runtime import app as APP
from module11.runtime.config import (BundleSource, ConfigInvalid, load_component, parse_runtime_config,
                                     resolve_bundle)
from module11.runtime.telemetry import RecordingBackend, parse_baggage
from test_module11_support import ROOT, config_doc, envelope, invoke, local_dir, make_runtime, stored_result

RUNTIME_MODULES = ["app", "config", "envelope", "fake_model", "flags", "guardrail", "loop", "model_retry", "results",
                   "telemetry", "tools"]


@pytest.fixture
def d(tmp_path):
    return local_dir(tmp_path)


# ---------------------------------------------------------------- M11_RUNTIME_CONFIG
def test_runtime_config_validates_and_bootstrap_needs_both_nulls(d):
    doc = config_doc(d)
    assert parse_runtime_config(json.dumps(doc)).bootstrap is False
    assert parse_runtime_config(json.dumps({**doc, "bundleArn": None, "bundleVersion": None})).bootstrap is True
    for bad in ({**doc, "bundleArn": None}, {**doc, "bundleVersion": None}, {**doc, "extra": 1},
                {k: v for k, v in doc.items() if k != "memoryId"}, {**doc, "runtimeDigest": "sha256:zz"},
                {**doc, "stage": "Prod"}, {**doc, "snapshot": {"uri": "s3://b/k"}}):
        with pytest.raises(ConfigInvalid):
            parse_runtime_config(json.dumps(bad))
    with pytest.raises(ConfigInvalid):
        parse_runtime_config("")
    with pytest.raises(ConfigInvalid):
        parse_runtime_config("{not json")


def test_agentcore_mode_refuses_local_locations(d):
    cfg = parse_runtime_config(json.dumps(config_doc(d)))
    with pytest.raises(ConfigInvalid, match="local data locations"):
        APP.Runtime(cfg, local=False, flags=object(), bundles=BundleSource(), backend=RecordingBackend())


# ---------------------------------------------------------------- bundle component m11.bundle/2
def test_harness_component_loads_and_derives_tools():
    bundle = load_component(M.bundle_component(ROOT), "v1")
    assert {t.name for t in bundle.tools_for("companion")} == {
        "get_deployment_history", "query_metrics", "search_runbooks", "get_service_logs", "record_incident_note",
        "delegate_recovery_planning"}
    assert {t.name for t in bundle.tools_for("planner")} == {"get_blast_radius", "check_rollback_readiness"}
    note = bundle.tools["record_incident_note"]
    assert note.mutating and note.shadow_result["shadow"] is True
    assert bundle.tools["delegate_recovery_planning"].requires_flag == "module10-recovery-planning"
    assert bundle.required_tools("companion") == {"get_deployment_history"}
    assert "$schema" not in bundle.tools["query_metrics"].definition()["parameters"]


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(schema="m11.bundle/1"),
    lambda c: c["prompts"].pop("planner"),
    lambda c: c["orchestration"].pop("maxTurns"),
    lambda c: c["tools"]["record_incident_note"]["meta"].pop("shadowResult"),
    lambda c: c["tools"]["query_metrics"]["meta"].update(name="other"),
    lambda c: c["models"]["companion"].update(maxOutputTokens=0),
])
def test_invalid_component_raises_config_invalid(mutate):
    component = M.bundle_component(ROOT)
    mutate(component)
    with pytest.raises(ConfigInvalid):
        load_component(component, "v1")


def test_bundle_cache_is_keyed_by_version():
    component = M.bundle_component(ROOT)
    calls = []

    def fetch(arn, version):
        calls.append(version)
        return {"components": {"arn:runtime": {"configuration": component}}}
    source = BundleSource(fetch)
    assert source.get("arn:b", "1") is source.get("arn:b", "1")
    source.get("arn:b", "2")
    assert calls == ["1", "2"]


def test_bootstrap_mode_answers_503(d):
    runtime = make_runtime(d, config=config_doc(d, "config-bootstrap.json"))
    with pytest.raises(APP.HttpError) as caught:
        runtime.prepare(envelope(d), {})
    assert (caught.value.status, caught.value.body) == (503, {"error": "bootstrapping"})


def test_invalid_bundle_answers_500_config_invalid(d):
    bundle_file = next((d / "bundles").glob("*.json"))
    doc = json.loads(bundle_file.read_text())
    next(iter(doc["components"].values()))["configuration"]["schema"] = "m11.bundle/0"
    bundle_file.write_text(json.dumps(doc))
    with pytest.raises(APP.HttpError) as caught:
        make_runtime(d).prepare(envelope(d), {})
    assert (caught.value.status, caught.value.body) == (500, {"error": "config_invalid"})


# ---------------------------------------------------------------- identity
IDENTITY_B = {"manifestVersion": "2026-10-21T09:00:00Z/fedcba9", "behaviorDigest": "sha256:" + "b" * 64,
              "release": "harness-v9"}


def test_identity_comes_from_the_envelope_and_runtime_digest_from_env(d):
    backend = RecordingBackend()
    runtime = make_runtime(d, backend=backend)
    env_a = envelope(d)
    env_b = envelope(d, **IDENTITY_B)
    env_b["request"]["request_id"] = "second-request"
    a, _ = invoke(runtime, env_a)
    b, _ = invoke(runtime, env_b)
    digest = config_doc(d)["runtimeDigest"]
    for body, ident in ((a, env_a["context"]), (b, IDENTITY_B)):
        assert body["status"] == "completed"
        for key in ("manifestVersion", "behaviorDigest", "release"):
            assert body["m11"][key] == ident[key]
        assert body["m11"]["runtimeDigest"] == digest
        assert body["behavior"]["version"] == ident["behaviorDigest"][7:23]
        spans = [s for s in backend.spans if s.trace_id == body["trace_id"]]
        assert spans and all(s.attributes["m11.manifest_version"] == ident["manifestVersion"]
                             and s.attributes["m11.behavior_digest"] == ident["behaviorDigest"]
                             and s.attributes["m11.release"] == ident["release"]
                             and s.attributes["m11.runtime_digest"] == digest for s in spans)
        note = next((d / "bucket" / "side-effects" / "local" / ident["release"]).glob("*/*.json"))
        stamp = json.loads(note.read_text())["stamp"]
        assert {k: stamp[k] for k in ("manifestVersion", "behaviorDigest", "release")} == \
            {k: ident[k] for k in ("manifestVersion", "behaviorDigest", "release")}
    records = [json.loads(line) for line in (d / "memory" / "records.jsonl").read_text().splitlines()]
    assert [r["metadata"]["manifestVersion"] for r in records] == [env_a["context"]["manifestVersion"],
                                                                   IDENTITY_B["manifestVersion"]]
    assert records[1]["namespaces"] == ["/devops-companion/local/release/harness-v9/"]
    assert stored_result(d, b)["m11"]["release"] == "harness-v9"


def test_tool_context_carries_identity_and_data(d):
    seen = []

    class Recording:
        def invoke(self, arn, event, timeout):
            seen.append(event["context"])
            from module11.runtime.tools import LocalInvoker
            return LocalInvoker().invoke(arn, event, timeout)
    body, _ = invoke(make_runtime(d, invoker=Recording()), envelope(d, **IDENTITY_B))
    assert body["status"] == "completed" and seen
    data = config_doc(d)
    for ctx in seen:
        assert {k: ctx[k] for k in IDENTITY_B} == IDENTITY_B
        assert ctx["data"] == {"snapshot": data["snapshot"], "evidence": data["evidence"],
                               "memoryId": data["memoryId"], "artifactsBucket": data["artifactsBucket"]}


# ---------------------------------------------------------------- baggage override
def test_baggage_overrides_the_env_pin_and_is_stamped(d):
    pinned = config_doc(d)["bundleVersion"]
    bundles = d / "bundles"
    (bundles / "override-7.json").write_text((bundles / f"{pinned}.json").read_text())
    backend = RecordingBackend()
    runtime = make_runtime(d, backend=backend)
    baggage = (f"aws.agentcore.configbundle_arn={bundles.as_uri()},aws.agentcore.configbundle_version=override-7,"
               "Self=abc,session.id=xyz")
    over, _ = invoke(runtime, envelope(d), {"baggage": baggage})
    plain_env = envelope(d)
    plain_env["request"]["request_id"] = "plain-1"
    plain, _ = invoke(runtime, plain_env, {"baggage": "Self=abc,session.id=xyz"})
    assert (over["m11"]["bundleVersion"], over["m11"]["bundleOverride"]) == ("override-7", True)
    assert (plain["m11"]["bundleVersion"], plain["m11"]["bundleOverride"]) == (pinned, False)
    for body, version, flag in ((over, "override-7", True), (plain, pinned, False)):
        spans = [s for s in backend.spans if s.trace_id == body["trace_id"]]
        assert spans and all(s.attributes["m11.bundle_version"] == version and s.attributes["m11.bundle_override"] is flag
                             for s in spans)


def test_baggage_parsing_and_resolution(d):
    entries = parse_baggage("aws.agentcore.configbundle_arn=arn%3Ax,aws.agentcore.configbundle_version=3;p=1, Self=s")
    assert entries["aws.agentcore.configbundle_version"] == "3" and entries["aws.agentcore.configbundle_arn"] == "arn:x"
    cfg = parse_runtime_config(json.dumps(config_doc(d)))
    assert resolve_bundle(cfg, entries) == ("arn:x", "3", True)
    assert resolve_bundle(cfg, {"Self": "s", "aws.agentcore.configbundle_version": "3"}) == (
        cfg["bundleArn"], cfg["bundleVersion"], False)


# ---------------------------------------------------------------- binding
def test_binding_refuses_non_loopback_without_agentcore():
    assert APP.bind_address(False, None, None) == ("127.0.0.1", 8091)
    assert APP.bind_address(False, "127.0.0.1", 9000) == ("127.0.0.1", 9000)
    assert APP.bind_address(True, "127.0.0.1", 1) == ("0.0.0.0", 8080)  # nosec B104 - asserts the bind rule; nothing binds
    for host in ("0.0.0.0", "192.168.1.5", "example.com", "::"):  # nosec B104 - asserts the bind rule; nothing binds
        with pytest.raises(SystemExit):
            APP.bind_address(False, host, None)


def test_main_refuses_bad_hosts_and_local_options_with_agentcore(d):
    with pytest.raises(SystemExit):
        APP.main(["--host", "0.0.0.0"])  # nosec B104 - asserts the bind rule; nothing binds
    with pytest.raises(SystemExit):
        APP.build_runtime(APP.argparse.Namespace(agentcore=True, config_file=str(d / "config.json"), fake_model=False))
    assert APP.main(["--config-file", str(d / "missing.json")]) == 1


# ---------------------------------------------------------------- HTTP (loopback, ephemeral port)
def _serve(runtime):
    APP.RUNTIME = runtime
    server = ThreadingHTTPServer(("127.0.0.1", 0), APP.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _post(url, payload):
    request = urllib.request.Request(url + "/invocations", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - the test's own loopback server
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_http_ping_bootstrap_invalid_and_stream(d):
    server, url = _serve(make_runtime(d))
    try:
        with urllib.request.urlopen(url + "/ping", timeout=10) as response:  # nosec B310 - the test's own loopback server
            assert response.status == 200 and json.loads(response.read())["status"] == "Healthy"
        assert _post(url, {"identity": {}})[0] == 400
        status, text = _post(url, envelope(d))
        assert status == 200
        names = re.findall(r"^event: (\w+)$", text, re.M)
        assert names[0] == "accepted" and names[-1] == "result" and "tool" in names
    finally:
        server.shutdown()
        server.server_close()
    server, url = _serve(make_runtime(d, config=config_doc(d, "config-bootstrap.json")))
    try:
        with urllib.request.urlopen(url + "/ping", timeout=10) as response:  # nosec B310 - the test's own loopback server
            assert response.status == 200
        assert _post(url, envelope(d)) == (503, json.dumps({"error": "bootstrapping"}))
    finally:
        server.shutdown()
        server.server_close()
        APP.RUNTIME = None


# ---------------------------------------------------------------- M13 and sampling
def test_every_runtime_module_imports_without_openai_and_opentelemetry():
    code = ("import sys\n"
            "for name in ('openai', 'opentelemetry', 'opentelemetry.trace', 'openai.providers'):\n"
            "    sys.modules[name] = None\n"
            f"for mod in {RUNTIME_MODULES!r}:\n"
            "    __import__('module11.runtime.' + mod)\n"
            "from module11.runtime.telemetry import default_backend, NoopBackend, parse_baggage\n"
            "assert isinstance(default_backend(), NoopBackend)\n"
            "assert parse_baggage('a=1') == {'a': '1'}\n"
            "print('ok')\n")
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this interpreter with fixed test code
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
    listed = sorted(p.stem for p in (ROOT / "module11/runtime").glob("*.py") if p.stem != "__init__")
    assert listed == sorted(RUNTIME_MODULES)


def test_dockerfile_samples_every_span_and_nothing_overrides_it():
    dockerfile = (ROOT / "module11/deployment/Dockerfile").read_text()
    assert re.search(r"^\s*OTEL_TRACES_SAMPLER=always_on\b", dockerfile, re.M)
    assert re.search(r"^FROM public\.ecr\.aws/docker/library/python:3\.13\.12-slim-bookworm$", dockerfile, re.M)
    assert "COPY . /app" in dockerfile and 'CMD ["opentelemetry-instrument", "python", "-m", "module11.runtime.app", "--agentcore"]' in dockerfile
    # No program, config model or env map sets the sampler as a key (config_model has its own case).
    setter = re.compile(r"""['"]OTEL_TRACES_SAMPLER['"]|OTEL_TRACES_SAMPLER\s*:""")
    offenders = [p for p in (ROOT / "module11").rglob("*") if p.is_file() and p.suffix in {".py", ".mjs", ".yaml", ".yml", ".json"}
                 and ".local" not in p.parts and setter.search(p.read_text(errors="ignore"))]
    assert offenders == []
