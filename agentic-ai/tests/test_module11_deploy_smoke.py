# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Smoke test and restore-on-smoke-failure of `release deploy`, with
botocore Stubber clients: endpoints go back first; then, on a stage with a /current, the flags
object is written back with IfMatch (412 -> SmokeFailedFlagsChanged) and `--reapply` runs for
edge_code, tool_permissions or infrastructure; a bootstrap stage restores endpoints only."""
import io
import json
from types import SimpleNamespace

import botocore.session
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from module11.aws import deploy as AD
from module11.release import deploy as D
from module11.release import detect as DT

RUNTIME = "m11_staging_companion-abc"


def _client(service):
    return botocore.session.get_session().create_client(service, region_name="us-east-1", aws_access_key_id="x",  # nosec B106 - dummy credentials for an offline, stubbed client
                                                        aws_secret_access_key="y")


def _plan(kinds):
    return DT.ChangePlan(None, None, list(kinds), False, [], False, "none", "smoke", {}, "default", "full", "deploy")


def _deploy(stage="staging"):
    d = AD.Deploy.__new__(AD.Deploy)
    d.stage, d.log = stage, (lambda line: None)
    d.ctx = SimpleNamespace(account="111122223333", region="us-east-1")
    d.s3, d.ctl = _client("s3"), _client("bedrock-agentcore-control")
    d.reapplied = 0

    def reapply():
        d.reapplied += 1
    d.reapply = reapply
    return d


def _endpoint(version):
    return {"liveVersion": str(version), "status": "READY", "agentRuntimeEndpointArn": "arn:e", "agentRuntimeArn": "arn:r",
            "createdAt": "2026-10-06T00:00:00Z", "lastUpdatedAt": "2026-10-06T00:00:00Z", "name": "staging", "id": "e"}


def _expect_retarget(stub, name, version):
    update = {k: v for k, v in _endpoint(version).items() if k not in ("name", "id")}
    stub.add_response("update_agent_runtime_endpoint", {**update, "targetVersion": str(version)},
                      {"agentRuntimeId": RUNTIME, "endpointName": name, "agentRuntimeVersion": str(version)})
    stub.add_response("get_agent_runtime_endpoint", _endpoint(version), {"agentRuntimeId": RUNTIME, "endpointName": name})


def _body(data: bytes):
    return StreamingBody(io.BytesIO(data), len(data))


@pytest.mark.parametrize("kinds, written, has_current, want", [
    (["instructions"], True, True, ["flags"]),
    (["edge_code"], False, True, ["reapply"]),
    (["tool_permissions", "flags"], True, True, ["flags", "reapply"]),
    (["infrastructure"], True, False, []),             # a bootstrap stage has no /current to restore to
    (["model"], False, True, []),
])
def test_restore_plan(kinds, written, has_current, want):
    assert D.restore_plan(kinds, flags_written=written, has_current=has_current) == want


def test_smoke_failure_restores_endpoints_flags_and_reapplies():
    d = _deploy()
    ctl, s3 = Stubber(d.ctl), Stubber(d.s3)
    _expect_retarget(ctl, "staging", 4)
    s3.add_response("get_object", {"Body": _body(b'{"flags": {}}'), "VersionId": "old"},
                    {"Bucket": "m11-staging-artifacts-111122223333-us-east-1", "Key": "config/flags.json", "VersionId": "old"})
    s3.add_response("put_object", {"ETag": '"restored"'},
                    {"Bucket": "m11-staging-artifacts-111122223333-us-east-1", "Key": "config/flags.json",
                     "Body": ANY, "IfMatch": '"new"', "ContentType": "application/json"})
    with ctl, s3:
        d._restore(D.SmokeFailed("smoke"), _plan(["edge_code"]), {"runtimeId": RUNTIME}, {"staging": "4"}, ["staging"],
                   {"written": True, "previousVersionId": "old", "newEtag": '"new"'}, {"deployment": {}}, "t", {})
    ctl.assert_no_pending_responses()
    s3.assert_no_pending_responses()
    assert d.reapplied == 1


def test_flags_changed_since_the_deploy_is_left_alone():
    d = _deploy()
    ctl, s3 = Stubber(d.ctl), Stubber(d.s3)
    _expect_retarget(ctl, "staging", 4)
    s3.add_response("get_object", {"Body": _body(b"{}"), "VersionId": "old"})
    s3.add_client_error("put_object", "PreconditionFailed", "At least one of the pre-conditions you specified did not hold",
                        http_status_code=412)
    with ctl, s3, pytest.raises(D.SmokeFailedFlagsChanged):
        d._restore(D.SmokeFailed("smoke"), _plan(["instructions"]), {"runtimeId": RUNTIME}, {"staging": "4"}, ["staging"],
                   {"written": True, "previousVersionId": "old", "newEtag": '"new"'}, {"deployment": {}}, "t", {})
    assert d.reapplied == 0


def test_bootstrap_stage_restores_endpoints_only():
    d = _deploy("prod")
    ctl, s3 = Stubber(d.ctl), Stubber(d.s3)
    _expect_retarget(ctl, "stable", 1)
    _expect_retarget(ctl, "candidate", 1)
    with ctl, s3:
        d._restore(D.SmokeFailed("smoke"), _plan(["infrastructure"]), {"runtimeId": RUNTIME},
                   {"stable": "1", "candidate": "1"}, ["stable", "candidate"], {"written": True, "previousVersionId": None},
                   None, "t", {})
    ctl.assert_no_pending_responses()
    s3.assert_no_pending_responses()
    assert d.reapplied == 0


def test_smoke_verdict():
    events = [("tool", {"tool": "get_deployment_history", "status": "completed"}),
              ("result", {"status": "completed", "events": [{"tool": "get_deployment_history", "status": "completed"},
                                                            {"tool": "query_metrics", "status": "failed"}]})]
    text = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events)
    ok = D.smoke_verdict(text, ["get_deployment_history"])
    assert ok.ok and ok.executed == ["get_deployment_history"]
    missing = D.smoke_verdict(text, ["get_deployment_history", "query_metrics"])
    assert not missing.ok and missing.missing == ["query_metrics"]
    blocked = D.smoke_verdict('event: result\ndata: {"status": "blocked", "events": []}\n\n', [])
    assert not blocked.ok and blocked.status == "blocked"
    assert D.smoke_verdict("", []).status == "no_result"


def test_smoke_envelope_is_an_eval_shadow_session():
    record = {"smoke": {"scenarioId": "incident-triage-014", "input": "502s", "team": "platform"},
              "manifestVersion": "2026-10-06T10:00:00Z/abcdef1", "behaviorDigest": "sha256:" + "a" * 64}
    env = D.smoke_envelope(record, "operator-abcdef1", "smoke-1")
    assert env["context"]["variant"] == "eval" and env["context"]["shadow"] is True
    assert env["context"]["release"] == "operator-abcdef1" and env["request"]["request_id"] == "smoke-1"


def test_edge_probes_require_401_and_200(monkeypatch):
    d = _deploy()
    answers = {"POST": 403, "OPTIONS": 200}

    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        code = answers[request.get_method()]
        if code >= 400:
            raise AD.urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        return Response(code)
    monkeypatch.setattr(AD.urllib.request, "urlopen", urlopen)
    with pytest.raises(D.SmokeFailed, match="POST 403"):
        d._edge_probes("https://api.example.com/api")
    answers["POST"] = 401
    d._edge_probes("https://api.example.com/api")


def test_full_current_takes_the_live_deployment_record(monkeypatch):
    """After PROMOTED the controller rewrites only the SSM form of /current (both endpoints on the promoted
    version); the S3 copy still holds the endpoints written at `open`. The next rollout's stable version
    comes from the SSM form, so `open` names the version the stable endpoint serves."""
    d = _deploy("dev")
    stale = {"manifestVersion": "m", "deployment": {"dev": {"runtimeVersion": "3", "endpoints": {"stable": "2",
                                                                                                "candidate": "3"}}},
             "artifacts": {"agent": "sha256:a"}}
    monkeypatch.setattr(AD.releases, "get", lambda s3, uri: json.dumps(stale).encode())
    live = {"manifestVersion": "m", "s3Uri": "s3://b/k",
            "deployment": {"dev": {"runtimeVersion": "3", "endpoints": {"stable": 3, "candidate": 3}}}}
    full = d._full(live)
    assert full["deployment"]["dev"]["endpoints"] == {"stable": 3, "candidate": 3}
    assert full["artifacts"] == {"agent": "sha256:a"}
    assert d._full(None) is None


def test_bundle_parent_is_the_branch_head():
    """After a ROLLED_BACK canary the newest `dev` version is the rolled-back candidate's; the service requires
    it as the first parent, so a later release names it rather than /applied's version."""
    from module11.aws import bundles

    def version(vid, at, branch, parent=None):
        return {"versionId": vid, "versionCreatedAt": at,
                "lineageMetadata": {"branchName": branch, **({"parentVersionIds": [parent]} if parent else {})}}

    class Ctl:
        def __init__(self, versions):
            self.versions = versions

        def list_configuration_bundle_versions(self, **kwargs):
            return {"versions": list(self.versions)}
    arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:configuration-bundle/m11_dev_companion_cfg-x"
    ctl = Ctl([version("init", "2026-10-06T01:00:00Z", None), version("live", "2026-10-06T02:00:00Z", "live-test", "init"),
               version("cand-2", "2026-10-07T11:04:00Z", "dev", "cand-1"), version("cand-1", "2026-10-07T11:00:00Z", "dev", "init")])
    assert bundles.branch_head(ctl, arn, "dev") == "cand-2"
    assert bundles.branch_head(ctl, arn, "staging") is None


def test_a_cleared_latch_does_not_block_a_deploy():
    """`latch clear` leaves the parameter with state cleared; only state latched refuses the digest."""
    from module11.aws import params

    class Ssm:
        def __init__(self, value):
            self.value = value

        def get_parameter(self, Name):
            return {"Parameter": {"Value": json.dumps(self.value)}}
    digest = "sha256:" + "ab" * 32
    assert params.latched(Ssm({"state": "latched", "rolloutId": "r-1"}), digest) == {"state": "latched", "rolloutId": "r-1"}
    assert params.latched(Ssm({"state": "cleared", "reason": "test data"}), digest) is None
