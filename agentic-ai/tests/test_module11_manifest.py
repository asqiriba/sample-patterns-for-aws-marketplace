# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/manifest.py."""
import copy
import json
from pathlib import Path

import pytest

from module11.release import manifest as M
from module11.release.hashing import sha256_bytes
from module11.release.naming import latch_param

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"
GIT = {"commit": "a7f3c91" + "0" * 33, "timestamp": "2026-10-20T14:02:11Z", "tag": "harness/v8",
       "subject": "Payments: go straight to logs"}
SHA = "sha256:" + "a" * 64
TOOLS = ["check_rollback_readiness", "get_blast_radius", "get_deployment_history", "get_service_logs",
         "query_metrics", "record_incident_note", "search_runbooks"]


@pytest.fixture(scope="module")
def declared():
    return M.build_declared(FIXTURE, GIT)


def _deployment(stage, version="14"):
    arn = "arn:aws:lambda:us-east-1:111122223333:function:m11-{s}-{t}:{v}"
    return {
        "account": "111122223333", "region": "us-east-1",
        "runtimeArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/m11_prod-AbCdEf1234",
        "runtimeVersion": version, "endpoints": {"stable": "13", "candidate": version},
        "bundle": {"arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:configuration-bundle/b-1",
                   "versionId": "0e075b1f-0000-4000-8000-000000000000", "componentSha256": SHA},
        "runtimeConfigSha256": SHA, "runtimeIdentity": SHA,
        "toolArns": {t: arn.format(s=stage, t=t, v=9) for t in TOOLS},
        "layerArn": "arn:aws:lambda:us-east-1:111122223333:layer:m11-sdk:3",
        "guardrail": {"id": "gr-abcdef123456", "version": "3"},
        "pinsUri": f"s3://m11-release-111122223333-us-east-1/release-inputs/{stage}/2026-10-20T14:02:11Z/a7f3c91.pins.json",
        "flagsObject": {"key": "config/flags.json", "versionId": "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY"}}


def test_declared_manifest_shape(declared):
    assert declared["schema"] == "m11.manifest/2"
    assert declared["manifestVersion"] == "2026-10-20T14:02:11Z/a7f3c91"
    assert declared["digests"]["artifactCount"] == 55 and len(declared["artifacts"]) == 55
    assert "deployment" not in declared
    assert set(declared["build"]["handlers"]) == set(TOOLS)
    assert declared["build"]["bundleComponent"]["s3Key"] == "releases/2026-10-20T14:02:11Z/a7f3c91/bundle-component.json"
    assert declared["model"]["companion"]["modelId"]
    assert M.build_declared(FIXTURE, GIT) == declared


def test_stage_deployment_sections_do_not_change_digests(declared):
    staging = {**copy.deepcopy(declared), "deployment": {"staging": _deployment("staging", "4")}}
    prod = {**copy.deepcopy(declared), "deployment": {"prod": _deployment("prod")}}
    M.validate(staging)
    M.validate(prod)
    assert staging["digests"] == prod["digests"] == declared["digests"]


def test_compact_form_fits_and_keeps_identity_fields(declared):
    prod = {**copy.deepcopy(declared), "deployment": {"prod": _deployment("prod")}}
    prod["build"]["imageDigest"] = SHA
    prod["guardrail"]["definitionSha256"] = SHA
    out = M.compact(prod, "s3://bucket/releases/x/manifest.json")
    assert "artifacts" not in out and out["artifactsSha256"].startswith("sha256:")
    raw = json.dumps(out, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(raw) <= M.MAX_COMPACT_BYTES
    assert out["build"]["bundleComponent"] and out["deployment"]["prod"]["runtimeConfigSha256"]
    assert out["deployment"]["prod"]["runtimeIdentity"]
    huge = copy.deepcopy(prod)
    huge["git"]["subject"] = "x" * 300
    huge["deployment"]["prod"]["toolArns"].update({f"t{i}": "arn:" + "y" * 200 for i in range(40)})
    with pytest.raises(M.ManifestTooLarge):
        M.compact(huge, "s3://bucket/x")


def test_schema_rejects_bad_manifests(declared):
    bad = copy.deepcopy(declared)
    bad["manifestVersion"] = "2026-10-20 14:02/a7f3c91"
    with pytest.raises(M.ManifestError):
        M.validate(bad)
    bad = copy.deepcopy(declared)
    del bad["digests"]["runtime"]
    with pytest.raises(M.ManifestError):
        M.validate(bad)


def test_latch_names_have_no_colon(declared):
    name = latch_param(declared["digests"]["behavior"])
    assert ":" not in name and name.startswith("/m11/devops-companion/latch/")


# ---------------------------------------------------------------- bundle component
def test_verify_component_accepts_the_sealed_object(declared):
    data = M.component_bytes(M.bundle_component(FIXTURE))
    assert sha256_bytes(data) == declared["build"]["bundleComponent"]["sha256"]
    assert M.verify_component(declared, data)["schema"] == "m11.bundle/2"


def test_verify_component_rejects_a_changed_object(declared):
    data = M.component_bytes(M.bundle_component(FIXTURE)) + b" "
    with pytest.raises(M.ReleaseInputsCorrupt, match="object sha256"):
        M.verify_component(declared, data)


@pytest.mark.parametrize("edit,artifact", [
    (lambda c: c["prompts"].__setitem__("companion", c["prompts"]["companion"] + "Skip runbooks.\n"),
     "instructions/companion"),
    (lambda c: c["tools"]["search_runbooks"].__setitem__("description", "Search anything at all, any time.\n"),
     "tool/search_runbooks/description"),
    (lambda c: c["models"]["companion"].__setitem__("maxOutputTokens", 4000), "model"),
])
def test_verify_component_names_the_edited_artifact(declared, edit, artifact):
    component = M.bundle_component(FIXTURE)
    edit(component)
    data = M.component_bytes(component)
    manifest = copy.deepcopy(declared)
    manifest["build"]["bundleComponent"]["sha256"] = sha256_bytes(data)  # object hash passes; content does not
    with pytest.raises(M.ReleaseInputsCorrupt, match=f"bundle component: {artifact}$"):
        M.verify_component(manifest, data)


# ---------------------------------------------------------------- bundle reuse
def _current(version, sha):
    return {"deployment": {"prod": {"bundle": {"versionId": version, "componentSha256": sha}}}}


def test_bundle_reuse_by_content():
    a, b, c = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64
    applied = {"bundleVersion": "v-applied", "bundleComponent": {"sha256": b}}
    assert M.bundle_reuse(a, _current("v-current", a), applied, "prod") == "v-current"
    assert M.bundle_reuse(b, _current("v-current", a), applied, "prod") == "v-applied"
    assert M.bundle_reuse(c, _current("v-current", a), applied, "prod") is None
    assert M.bundle_reuse(a, None, None, "prod") is None


def test_bundle_reuse_after_rollback_uses_current_never_candidate():
    stable, candidate = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    applied = {"bundleVersion": "v-candidate", "bundleComponent": {"sha256": candidate}}  # /applied on the candidate
    # A flags-only release carries stable's component (bundle-rule artifacts unchanged vs /current).
    assert M.bundle_reuse(stable, _current("v-stable", stable), applied, "prod") == "v-stable"


# ---------------------------------------------------------------- runtime identity
def _config(tool_version="9", guardrail_version="3", bundle_version="v-1", **over):
    env = {"stage": "prod", "bundleArn": "arn:b", "bundleVersion": bundle_version,
           "toolArns": {t: f"arn:aws:lambda:us-east-1:111122223333:function:m11-prod-{t}:{tool_version}" for t in TOOLS},
           "guardrail": {"id": "gr-1", "version": guardrail_version}, "runtimeDigest": SHA}
    cfg = {"agentRuntimeArn": "arn:rt", "agentRuntimeId": "m11_prod-x", "agentRuntimeName": "m11_prod",
           "agentRuntimeVersion": "14", "status": "READY", "createdAt": "2026-10-20T14:00:00Z",
           "lastUpdatedAt": "2026-10-20T14:05:00Z", "platformVersion": "1",
           "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "123.dkr.ecr/x@sha256:" + "d" * 64}},
           "roleArn": "arn:aws:iam::111122223333:role/m11-prod-runtime",
           "networkConfiguration": {"networkMode": "PUBLIC"}, "protocolConfiguration": {"serverProtocol": "HTTP"},
           "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 900},
           "environmentVariables": {M.RUNTIME_CONFIG_ENV: json.dumps(env)}}
    cfg.update(over)
    return cfg


def _manifest():
    return {"build": {"handlers": {t: {"codeSha256": f"code-{t}"} for t in TOOLS},
                      "bundleComponent": {"sha256": SHA}},
            "guardrail": {"definitionSha256": "sha256:" + "e" * 64},
            "deployment": {"prod": {"bundle": {"componentSha256": SHA}}}}


def test_version_numbers_change_config_sha_but_not_identity():
    a = M.runtime_ids(_config(), _manifest(), "prod")
    b = M.runtime_ids(_config(tool_version="12", guardrail_version="4", bundle_version="v-2"), _manifest(), "prod")
    assert a[0] != b[0] and a[1] == b[1]


@pytest.mark.parametrize("over", [
    {"lifecycleConfiguration": {"idleRuntimeSessionTimeout": 120, "maxLifetime": 900}},
    {"agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "123.dkr.ecr/x@sha256:" + "f" * 64}}},
    {"requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Amzn-Bedrock-AgentCore-Runtime-Custom-A"]}},
    {"authorizerConfiguration": {"customJWTAuthorizer": {"discoveryUrl": "https://x"}}},
    {"metadataConfiguration": {"a": 1}}, {"filesystemConfigurations": [{"a": 1}]},
    {"capacityProviderConfiguration": {"a": 1}}, {"description": "edited by hand"},
])
def test_caller_settable_changes_change_both_hashes(over):
    a = M.runtime_ids(_config(), _manifest(), "prod")
    b = M.runtime_ids(_config(**over), _manifest(), "prod")
    assert a[0] != b[0] and a[1] != b[1]


def test_env_field_change_changes_identity():
    cfg = _config()
    env = json.loads(cfg["environmentVariables"][M.RUNTIME_CONFIG_ENV])
    env["memoryId"] = "mem-other"
    cfg["environmentVariables"] = {M.RUNTIME_CONFIG_ENV: json.dumps(env)}
    assert M.runtime_ids(cfg, _manifest(), "prod")[1] != M.runtime_ids(_config(), _manifest(), "prod")[1]


@pytest.mark.parametrize("over", [{"status": "UPDATING"}, {"createdAt": "x"}, {"lastUpdatedAt": "y"},
                                  {"failureReason": "z"}, {"platformVersion": "2"},
                                  {"workloadIdentityDetails": {"workloadIdentityArn": "arn:w"}}])
def test_service_fields_change_neither_hash(over):
    assert M.runtime_ids(_config(**over), _manifest(), "prod") == M.runtime_ids(_config(), _manifest(), "prod")


def test_member_constant_matches_botocore_model():
    botocore = pytest.importorskip("botocore.session")
    model = botocore.get_session().get_service_model("bedrock-agentcore-control")
    get = set(model.operation_model("GetAgentRuntime").output_shape.members)
    update = set(model.operation_model("UpdateAgentRuntime").input_shape.members)
    assert set(M.RUNTIME_CONFIG_MEMBERS) == (get & update) - {"agentRuntimeId", "platformVersion"}
    assert len(M.RUNTIME_CONFIG_MEMBERS) == 12
