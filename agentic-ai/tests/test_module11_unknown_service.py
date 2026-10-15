# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A wrong service name is a recoverable `not_found` that lists the known services, never an opaque `internal`
error (seen live: on unchanged prod the model began calling the evidence tools with "checkout" for checkout-service;
the tool role may read but not list evidence/*, so S3 answered 403 and every tool returned `internal ClientError`).
Real AWS errors still surface as errors with their code. Seal and the local data directory write the index."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from module11.aws import evidence as O
from module11.aws import objects

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("check_rollback_readiness", "get_blast_radius", "get_deployment_history", "get_service_logs",
         "query_metrics")
SHA = "sha256:" + "a" * 64


def evidence(tmp_path: Path, *, index: bool = True) -> dict:
    base = tmp_path / "evidence"
    (base / "incidents").mkdir(parents=True)
    meta = json.dumps({"m11-sha256": SHA})
    for service in ("checkout-service", "payments-service"):
        f = base / "incidents" / f"{service}.json"
        f.write_text(json.dumps({"service": service, "deployment": {"deployment_id": "d-1"}}))
        Path(str(f) + ".metadata.json").write_text(meta)
    if index:
        (base / "index.json").write_text(json.dumps({"services": ["payments-service", "checkout-service"]}))
        Path(str(base / "index.json") + ".metadata.json").write_text(meta)
    return {"evidence": {"uri": base.as_uri() + "/", "sha256": SHA}}


@pytest.fixture(autouse=True)
def fresh_index_cache():
    O._KNOWN.clear()
    yield
    O._KNOWN.clear()


def test_a_wrong_name_lists_the_known_services(tmp_path):
    data = evidence(tmp_path)
    with pytest.raises(O.UnknownService) as err:
        O.read_incident(data, "cart")
    assert err.value.code == "not_found" and isinstance(err.value, LookupError)
    assert err.value.public_message == "no evidence for service 'cart'; known services: checkout-service, payments-service"
    incident, _ = O.read_incident(data, "checkout-service")
    assert incident["service"] == "checkout-service"


@pytest.mark.parametrize("name", ["checkout", "Checkout", "CHECKOUT-SERVICE", "Payments"])
def test_a_name_that_names_exactly_one_service_resolves_to_it(name, tmp_path):
    """v26 staging gate: "checkout" -> not_found -> a retry with "checkout-service" added a call that
    TrajectoryExactOrderMatch counts. A unique short or differently cased name now reads the service directly."""
    data = evidence(tmp_path)
    canonical = "payments-service" if name == "Payments" else "checkout-service"
    assert O.resolve_service(data, name) == canonical
    incident, _ = O.read_incident(data, name)
    assert incident["service"] == canonical


def test_an_ambiguous_name_is_not_found_with_the_list(tmp_path):
    data = evidence(tmp_path)
    index = Path(data["evidence"]["uri"].removeprefix("file://")) / "index.json"
    index.write_text(json.dumps({"services": ["checkout", "checkout-service", "payments-service"]}))
    assert O.resolve_service(data, "checkout") == "checkout"          # an exact name is never re-resolved
    with pytest.raises(O.UnknownService) as err:
        O.resolve_service(data, "Checkout")                           # "checkout" and "checkout-service" both match
    assert err.value.known == ["checkout", "checkout-service", "payments-service"]
    with pytest.raises(O.UnknownService):
        O.resolve_service(data, "service")                            # a fragment is not a prefix match


def test_the_index_is_read_once_per_snapshot(tmp_path, monkeypatch):
    data = evidence(tmp_path)
    reads = []
    real = O.verified_get
    monkeypatch.setattr(O, "verified_get", lambda uri, sha: reads.append(uri) or real(uri, sha))
    for name in ("checkout", "checkout-service", "payments-service"):
        O.read_incident(data, name)
    assert sum(1 for uri in reads if uri.endswith("/index.json")) == 1


def test_a_snapshot_without_an_index_keeps_the_old_behavior(tmp_path):
    data = evidence(tmp_path, index=False)
    assert O.known_services(data["evidence"]) is None
    with pytest.raises(objects.ObjectNotFound) as err:
        O.read_incident(data, "checkout")
    assert not isinstance(err.value, O.UnknownService)


class Denied(Exception):
    def __init__(self, code):
        super().__init__("An error occurred (403) when calling the HeadObject operation: Forbidden")
        self.response = {"Error": {"Code": code, "Message": "secret request detail"}}


def test_an_index_s3_denial_falls_back_and_other_errors_are_raised(monkeypatch):
    ref = {"uri": "s3://bucket/evidence/abc/", "sha256": SHA}
    monkeypatch.setattr(O, "verified_get", lambda uri, sha: (_ for _ in ()).throw(Denied("403")))
    assert O.known_services(ref) is None
    O._KNOWN.clear()                                   # a denial is cached for the snapshot like any answer
    monkeypatch.setattr(O, "verified_get", lambda uri, sha: (_ for _ in ()).throw(Denied("SlowDown")))
    with pytest.raises(Denied):
        O.known_services(ref)


def test_internal_errors_carry_the_aws_code_but_never_its_message():
    assert O.tool_error_message(Denied("AccessDenied")) == "Denied AccessDenied"
    assert O.tool_error_message(RuntimeError("detail")) == "RuntimeError"


def _handler(tool):
    path = ROOT / f"module11/harness/agent/tools/{tool}/handler/index.py"
    spec = importlib.util.spec_from_file_location(f"m11_{tool}_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tool", TOOLS)
def test_every_evidence_tool_answers_a_wrong_name_with_the_known_services(tool, tmp_path, monkeypatch):
    handler = _handler(tool)
    data = {**evidence(tmp_path), "artifactsBucket": tmp_path.as_uri()}
    monkeypatch.setattr(handler.logic, "run", lambda *a, **k: O.read_incident(k["data"], "cart"))
    out = handler.handler({"arguments": {"service_name": "cart"}, "context": {"data": data}}, None)
    assert out == {"ok": False, "error": {"code": "not_found", "message": "no evidence for service 'cart'; "
                                                                        "known services: checkout-service, payments-service"}}
    monkeypatch.setattr(handler.logic, "run", lambda *a, **k: (_ for _ in ()).throw(Denied("AccessDenied")))
    out = handler.handler({"arguments": {"service_name": "checkout-service"}, "context": {"data": data}}, None)
    assert out == {"ok": False, "error": {"code": "internal", "message": "Denied AccessDenied"}}


def sealed_evidence(tmp_path: Path) -> dict:
    """The real sealed evidence (Seal's objects), laid out as a file:// snapshot."""
    from module11.release import seal as SEAL
    root = tmp_path / "bucket"
    for o in SEAL._evidence(ROOT, SHA):
        f = root / o.key
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(o.body)
        Path(str(f) + ".metadata.json").write_text(json.dumps(o.metadata))
    prefix = SEAL._evidence(ROOT, SHA)[0].key.rsplit("/", 1)[0]
    return {"evidence": {"uri": (root / prefix).as_uri() + "/", "sha256": SHA}, "artifactsBucket": root.as_uri()}


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("name", ["checkout", "Checkout-Service"])
def test_every_evidence_tool_answers_with_the_canonical_service(tool, name, tmp_path):
    handler = _handler(tool)
    out = handler.handler({"arguments": {"service_name": name}, "context": {"data": sealed_evidence(tmp_path)}}, None)
    assert out["ok"] is True, out
    assert out["result"]["service"] == "checkout-service"


def test_the_mutating_tool_never_aliases():
    """record_incident_note writes what the model sent: it does not import the resolver, and its closure (check C12)
    does not include it, so a note is never filed under a service the model did not name."""
    logic = (ROOT / "module11/harness/agent/tools/record_incident_note/handler/logic.py").read_text()
    closure = (ROOT / "module11/harness/pipeline/closures/handler-record_incident_note.txt").read_text().split()
    assert "evidence" not in logic.split("from module11.aws import", 1)[1].splitlines()[0]
    assert "module11/aws/evidence.py" not in closure


def test_seal_writes_the_index_beside_the_incidents():
    from module11.release import seal as SEAL
    objects = SEAL._evidence(ROOT, SHA)
    index = [o for o in objects if o.key.endswith("/index.json")]
    assert len(index) == 1 and index[0].metadata == {"m11-sha256": SHA}
    services = json.loads(index[0].body)["services"]
    assert services == sorted(o.key.rsplit("/", 1)[-1][:-5] for o in objects if "/incidents/" in o.key)
    assert "checkout-service" in services
