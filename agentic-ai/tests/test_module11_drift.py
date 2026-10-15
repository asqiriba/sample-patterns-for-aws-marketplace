# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Drift checker."""
import copy
import json
import urllib.parse
from pathlib import Path

import pytest

from module11.release import audit
from module11.release import controller as ctl
from module11.release import drift as D
from module11.release import guardrail as G
from module11.release import manifest as M

ROOT = Path(__file__).resolve().parents[1]
GUARDRAIL = json.loads((ROOT / "tests/fixtures/module11/get_guardrail.json").read_text(encoding="utf-8"))
RUNTIME_ENV = json.dumps({"toolArns": {"query_metrics": "arn:aws:lambda:us-east-1:111122223333:function:m11-prod-query_metrics:3"},
                          "bundleVersion": "bv-1", "runtimeDigest": "sha256:" + "d" * 64})


def runtime_config(**over):
    config = {
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "ecr/m11@sha256:" + "e" * 64}},
        "roleArn": "arn:aws:iam::111122223333:role/m11-prod-runtime",
        "networkConfiguration": {"networkMode": "PUBLIC"}, "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": {"M11_RUNTIME_CONFIG": RUNTIME_ENV, "LOG_LEVEL": "INFO"},
        "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 60, "maxLifetime": 900},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["x-m11"]}, "description": "m11 prod",
        "status": "READY", "lastUpdatedAt": "2026-10-13T00:00:00Z", "platformVersion": "1",
        "agentRuntimeId": "m11_prod-abc", "agentRuntimeVersion": "7",
    }
    config.update(over)
    return config


POLICY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "B", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::m11-release/evidence/*"},
    {"Sid": "A", "Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": ["arn:x:2", "arn:x:1"]}]}


def manifest(mv="2026-10-12T09:14:55Z/3c8e1b0", stable=7, candidate=7, runtime_digest="sha256:" + "d" * 64):
    doc = {"manifestVersion": mv, "digests": {"runtime": runtime_digest},
           "build": {"bundleComponent": {"sha256": "sha256:" + "c" * 64},
                     "handlers": {"query_metrics": {"codeSha256": "abc="}}},
           "guardrail": {"definitionSha256": G.response_sha256(GUARDRAIL)},
           "flags": {"sha256": "sha256:" + "f" * 64},
           "retrieval": [{"corpus": "runbooks", "snapshotSha256": "sha256:" + "9" * 64}],
           "deployment": {"prod": {"runtimeVersion": str(stable), "endpoints": {"stable": stable, "candidate": candidate}}}}
    doc["deployment"]["prod"]["runtimeConfigSha256"] = M.runtime_ids(runtime_config(), doc, "prod")[0]
    return doc


CURRENT = manifest()
LEDGER = [(audit.ledger_key("prod", 1000.0, "u1"), {"kind": "initial", "versionId": "bv-0"}),
          (audit.ledger_key("prod", 2000.0, "u2"), {"kind": "bundle-version", "versionId": "bv-1"})]


def observed(**over):
    obs = {
        "endpoints": {"stable": {"liveVersion": 7, "status": "READY"}, "candidate": {"liveVersion": 7, "status": "READY"}},
        "runtimeVersions": {"7": runtime_config()},
        "bundle": {"pinnedComponentSha256": "sha256:" + "c" * 64, "versions": ["bv-0", "bv-1"]},
        "tools": {"query_metrics": {"codeSha256": "abc="}},
        "runtimeToolArns": {"query_metrics": "arn:aws:lambda:us-east-1:111122223333:function:m11-prod-query_metrics:3"},
        "policies": {"m11-prod-runtime": urllib.parse.quote(json.dumps(POLICY))},
        "guardrail": GUARDRAIL, "flagsSha256": "sha256:" + "f" * 64, "snapshots": {"runbooks": "sha256:" + "9" * 64},
        "outcomes": [{"requestId": "r1", "manifestVersion": CURRENT["manifestVersion"], "runtimeDigest": "sha256:" + "d" * 64,
                      "startedAt": 5000.0}],
        "manifests": {CURRENT["manifestVersion"]: CURRENT},
    }
    obs.update(over)
    return obs


APPLIED = {"policyDigests": {"m11-prod-runtime": D.policy_digest(POLICY)}}
PINS = {"own": {"toolArns": {"query_metrics": "arn:aws:lambda:us-east-1:111122223333:function:m11-prod-query_metrics:3"}}}
IDLE = {"state": "IDLE", "endpointsWrittenAt": 100.0}


def check(current=CURRENT, candidate=None, control=IDLE, obs=None, ledger=LEDGER):
    return D.check("prod", current, candidate, control, obs if obs is not None else observed(), ledger, APPLIED, PINS,
                   "2026-10-13T19:00:00Z")


def components(report):
    return sorted((f.component, f.field) for f in report.findings)


def test_clean_state_gives_zero_findings():
    assert check().findings == []


def test_endpoint_updating_reports_status():
    obs = observed(endpoints={"stable": {"liveVersion": 14, "targetVersion": 12, "status": "UPDATING"}})
    current = manifest(stable=14, candidate=14)
    report = check(current=current, obs={**obs, "runtimeVersions": {}})
    assert [f.text() for f in report.findings if f.component == "endpoint"] == [
        "endpoint.stable: target 12 (UPDATING) != declared 14"]
    live = observed(endpoints={"candidate": {"liveVersion": 6, "status": "READY"}})
    assert components(check(obs=live)) == [("endpoint", "candidate")]


def test_flags_only_full_release_reports_no_runtime_drift_but_an_env_edit_does():
    moved = manifest(mv="2026-10-13T10:00:00Z/1234567")       # /current moved, same runtime version
    obs = observed(manifests={moved["manifestVersion"]: moved},
                   outcomes=[{"requestId": "r", "manifestVersion": moved["manifestVersion"],
                              "runtimeDigest": "sha256:" + "d" * 64, "startedAt": 5000.0}])
    assert check(current=moved, obs=obs).findings == []
    edited = runtime_config(environmentVariables={"M11_RUNTIME_CONFIG": RUNTIME_ENV, "LOG_LEVEL": "DEBUG"})
    assert components(check(obs=observed(runtimeVersions={"7": edited}))) == [("runtime", "v7.runtimeConfigSha256")]


@pytest.mark.parametrize("field,value", [
    ("requestHeaderConfiguration", {"requestHeaderAllowlist": ["x-other"]}),
    ("authorizerConfiguration", {"customJWTAuthorizer": {"discoveryUrl": "https://x"}}),
    ("metadataConfiguration", {"x": 1}),
    ("filesystemConfigurations", [{"x": 1}]),
])
def test_full_runtime_configuration_hand_edits_are_drift(field, value):
    report = check(obs=observed(runtimeVersions={"7": runtime_config(**{field: value})}))
    assert components(report) == [("runtime", "v7.runtimeConfigSha256")]


@pytest.mark.parametrize("field,value", [("status", "UPDATING"), ("lastUpdatedAt", "2027-01-01T00:00:00Z"),
                                         ("platformVersion", "2")])
def test_service_fields_are_not_drift(field, value):
    assert check(obs=observed(runtimeVersions={"7": runtime_config(**{field: value})})).findings == []


def test_stamping_mismatch_and_grace_window():
    bad = {"requestId": "r9", "manifestVersion": CURRENT["manifestVersion"], "runtimeDigest": "sha256:" + "0" * 64,
           "startedAt": 5000.0}
    assert components(check(obs=observed(outcomes=[bad]))) == [("stamping", "r9")]
    early = {**bad, "startedAt": 130.0}                       # within 60 s of the endpoints write
    assert check(obs=observed(outcomes=[early])).findings == []


def test_shadow_items_are_skipped_and_no_experiment_manifest_is_read():
    class Manifests(dict):
        def get(self, key, default=None):
            assert not str(key).startswith("releases/experiments/"), "experiment manifest read"
            return super().get(key, default)

    shadow = {"pk": "ROLLOUT#r-0123456789abcdef", "shadow": True, "originalRequestId": "r1",
              "runtimeSessionId": "m11-prod-candidate-v8-x", "startedAt": 5000.0}
    bad = {"requestId": "r2", "manifestVersion": CURRENT["manifestVersion"], "runtimeDigest": "sha256:" + "0" * 64,
           "startedAt": 5000.0}
    obs = observed(outcomes=[shadow, bad], manifests=Manifests({CURRENT["manifestVersion"]: CURRENT}))
    experiment = {"state": "SHADOWING", "endpointsWrittenAt": 100.0, "experiment": True}
    report = check(control=experiment, obs=obs)
    assert components(report) == [("stamping", "r2")]


def test_ledger_listing_order_and_unexpected_versions():
    same_ms = [(audit.ledger_key("prod", 3000.0, "aaaa"), {"kind": "bundle-version", "versionId": "bv-2"}),
               (audit.ledger_key("prod", 3000.0, "bbbb"), {"kind": "bundle-version", "versionId": "bv-3"})]
    ledger = LEDGER + list(reversed(same_ms))
    assert audit.sort_entries([k for k, _ in ledger])[-2:] == [k for k, _ in same_ms]
    assert D.ledger_versions(ledger) == {"bv-0", "bv-1", "bv-2", "bv-3"}
    obs = observed(bundle={"pinnedComponentSha256": "sha256:" + "c" * 64, "versions": ["bv-0", "bv-1", "bv-3"]})
    assert check(obs=obs, ledger=ledger).findings == []
    stray = observed(bundle={"pinnedComponentSha256": "sha256:" + "c" * 64, "versions": ["bv-0", "bv-9"]})
    report = check(obs=stray)
    assert [(f.component, f.observed, f.status) for f in report.findings] == [("bundle", "bv-9", "unexpected version")]
    assert audit.stamp(1792504931.123456) == "20261020T140211.123456Z"


def test_edited_role_policy_is_drift_reordering_is_not():
    reordered = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Sid": "A", "Resource": ["arn:x:1", "arn:x:2"], "Action": "lambda:InvokeFunction"},
        POLICY["Statement"][0]]}
    assert check(obs=observed(policies={"m11-prod-runtime": reordered})).findings == []
    edited = copy.deepcopy(POLICY)
    edited["Statement"][0]["Resource"] = "arn:aws:s3:::m11-release/*"
    assert components(check(obs=observed(policies={"m11-prod-runtime": edited}))) == [("iam", "m11-prod-runtime")]


def test_guardrail_flags_snapshot_and_tool_findings():
    edited = copy.deepcopy(GUARDRAIL)
    edited["contentPolicy"]["filters"][0]["inputStrength"] = "NONE"
    obs = observed(guardrail=edited, flagsSha256="sha256:" + "1" * 64, snapshots={"runbooks": "sha256:" + "2" * 64},
                   tools={"query_metrics": {"codeSha256": "zzz="}},
                   runtimeToolArns={"query_metrics": "arn:aws:lambda:us-east-1:111122223333:function:m11-prod-query_metrics:4"})
    assert components(check(obs=obs)) == [("flags", "sha256"), ("guardrail", "definitionSha256"),
                                          ("snapshot", "runbooks"), ("tool", "query_metrics.codeSha256"),
                                          ("tool", "toolArns")]


def test_candidate_is_checked_against_candidate_during_a_rollout():
    candidate = manifest(mv="2026-10-13T19:00:00Z/abcdef0", stable=7, candidate=8)
    obs = observed(endpoints={"stable": {"liveVersion": 7, "status": "READY"},
                              "candidate": {"liveVersion": 8, "status": "READY"}}, runtimeVersions={})
    rollout = {"state": "OBSERVING", "endpointsWrittenAt": 100.0}
    assert check(candidate=candidate, control=rollout, obs=obs).findings == []
    assert components(check(candidate=candidate, control=IDLE, obs=obs)) == [("endpoint", "candidate")]


def test_clean_after_promotion():
    candidate = manifest(mv="2026-10-13T19:00:00Z/abcdef0", stable=7, candidate=8)
    promoted = ctl.promoted_manifest(candidate, "prod", 8)
    obs = observed(endpoints={"stable": {"liveVersion": 8, "status": "READY"},
                              "candidate": {"liveVersion": 8, "status": "READY"}}, runtimeVersions={})
    done = {"state": "PROMOTED", "endpointsWrittenAt": 100.0}
    assert check(current=promoted, candidate={"state": "none"}, control=done, obs=obs).findings == []
    assert components(check(current=candidate, candidate={"state": "none"}, control=done, obs=obs)) == [
        ("endpoint", "stable")]


def test_external_probes_are_warnings():
    warnings = D.external_warnings({"openai.gpt-x": "LEGACY", "other": "ACTIVE"}, ("sha256:a", "sha256:b"),
                                   [("staging-ab-gateway/companion", "sha256:1", "sha256:2")])
    assert {f.severity for f in warnings} == {"warning"} and len(warnings) == 3


def test_revert_waits_out_updating_and_retries_conflicts():
    states = iter([{"status": "UPDATING"}, {"status": "UPDATING"}, {"status": "READY", "liveVersion": 12},
                   {"status": "UPDATING", "liveVersion": 12, "targetVersion": 14},
                   {"status": "READY", "liveVersion": 14}])
    calls, sleeps = [], []

    def update(version):
        calls.append(version)
        if len(calls) < 3:
            raise D.ConflictException()

    obs = D.revert_endpoint(lambda: next(states), update, 14, sleep=sleeps.append)
    assert obs["liveVersion"] == 14 and calls == [14, 14, 14]
    assert sleeps == [5.0, 5.0, 5.0, 5.0, 5.0]


def test_a_single_endpoint_stage_is_checked_against_current():
    """Staging has one endpoint, `staging`; it follows /current. A hand retarget of it is drift (seen live)."""
    doc = manifest()
    doc["deployment"] = {"staging": {"runtimeVersion": "7", "endpoints": {"staging": 7},
                                     "runtimeConfigSha256": doc["deployment"]["prod"]["runtimeConfigSha256"]}}
    obs = observed(endpoints={"staging": {"liveVersion": 6, "status": "READY"}}, outcomes=[], runtimeVersions={"7": runtime_config()})
    report = D.check("staging", doc, None, {"state": "IDLE"}, obs, LEDGER, APPLIED, PINS, "2026-10-13T19:00:00Z")
    assert [(f.component, f.field, f.observed) for f in report.findings if f.component == "endpoint"] == \
        [("endpoint", "staging", "live 6")]
    clean = D.check("staging", doc, None, {"state": "IDLE"},
                    observed(endpoints={"staging": {"liveVersion": 7, "status": "READY"}}, outcomes=[]), LEDGER, APPLIED,
                    PINS, "2026-10-13T19:00:00Z")
    assert [f for f in clean.findings if f.component == "endpoint"] == []
