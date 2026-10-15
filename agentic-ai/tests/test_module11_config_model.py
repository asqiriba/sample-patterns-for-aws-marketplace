# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Typed config for m11-harness: stage settings, release inputs, the runtime
config size limit, the bootstrap pair, BundleArnMismatch and the apply guard. Offline."""
import json

import pytest

from module11.infrastructure.harness import config_model as CM
from module11.release import pulumi_inputs as PI

SHA = "sha256:" + "b" * 64
OPERATOR = "arn:aws:iam::111122223333:user/operator"


def settings(stage="prod", **over):
    values = {"stage": stage, "accountId": "111122223333", "region": "us-east-1", "operatorPrincipals": [OPERATOR],
              **({"uiOrigin": "https://ui.example.com"} if stage == "prod" else {}), **over}
    return CM.settings(lambda k: values.get(k), stack=stage)


def inputs(**over):
    doc = {"imageDigest": SHA, "bundleArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:configuration-bundle/b-1",
           "bundleVersion": "v-1", "bundleComponent": {"uri": "s3://r/c.json", "sha256": SHA},
           "handlers": {}, "layer": {"s3Key": "layers/x.zip", "sha256": SHA}, "edge": {"s3Key": "handlers/edge/x.zip", "sha256": SHA},
           "evaluatorCode": {"s3Key": "handlers/e/x.zip", "codeSha256": "x="}, "flagsSha256": SHA,
           "snapshot": {"id": "pol-1", "uri": "s3://r/snapshots/policy/pol-1.json", "sha256": SHA},
           "evidence": {"uri": "s3://r/evidence/aa/", "sha256": SHA}, "pulumiInputs": {"uri": "s3://r/p.tar", "sha256": SHA},
           "retainedPins": {"toolArns": [], "modelResources": [], "deadlineSeconds": 150, "toolStatements": {}},
           "runtimeDigest": SHA, "manifestVersion": "2026-10-06T10:00:00Z/abcdef1", "behaviorDigest": SHA}
    doc.update(over)
    return PI.ReleaseInputs.from_dict(doc)


def test_stage_defaults_and_demo_labels():
    prod = settings()
    assert prod.endpoints == ["stable", "candidate"] and prod.teamConcurrency == 14
    assert prod.onlineEval["samplingPct"] == 100 and prod.rolloutProfile == "demo"
    assert "teamConcurrency" in prod.demoLabels and "default 10" in prod.demoLabels["teamConcurrency"]
    staging = settings("staging")
    assert staging.endpoints == ["staging"] and staging.teamConcurrency == 3
    mr = settings("mr-7", uiPreview="pending")
    assert mr.endpoints == ["preview"] and mr.stagekey == "mr7" and mr.is_mr
    assert settings(accountId=111122223333).accountId == "111122223333"   # JSON-decoded config value


@pytest.mark.parametrize("over, message", [
    ({"accountId": "1234"}, "12 digits"), ({"endpoints": ["stable"]}, "endpoints"),
    ({"teamConcurrency": 0}, "teamConcurrency"), ({"rolloutProfile": "fast"}, "rolloutProfile"),
    ({"operatorPrincipals": ["root"]}, "operatorPrincipals"), ({"uiOrigin": "http://x"}, "uiOrigin"),
    ({"onlineEval": {"samplingPct": 120}}, "samplingPct"),
])
def test_settings_rejects(over, message):
    with pytest.raises(CM.ConfigError, match=message):
        settings(**over)


def test_stage_must_match_the_stack():
    with pytest.raises(CM.ConfigError, match="differs"):
        CM.settings(lambda k: {"stage": "prod", "accountId": "111122223333", "region": "us-east-1"}.get(k), stack="dev")


def test_release_inputs_missing_names_the_command():
    with pytest.raises(PI.ReleaseInputsMissing, match="release deploy --stage staging"):
        CM.load_inputs("staging", {}, None, lambda s: None, None)


def test_runtime_config_has_runtime_digest_and_no_release_identity():
    config = CM.runtime_config(settings=settings(), inputs=inputs(), tool_arns={"t": "arn:t:1"}, guardrail_id="g",
                               guardrail_version="3", artifacts_bucket="b", memory_id="m")
    assert config["runtimeDigest"] == SHA and "manifestVersion" not in config and "behaviorDigest" not in config
    assert len(CM.serialize_runtime_config(config)) < CM.MAX_RUNTIME_CONFIG_CHARS


def test_runtime_config_too_large():
    arns = {f"tool_{i:02d}": f"arn:aws:lambda:us-east-1:111122223333:function:m11-prod-tool_{i:02d}:{i}" for i in range(80)}
    config = CM.runtime_config(settings=settings(), inputs=inputs(), tool_arns=arns, guardrail_id="g",
                               guardrail_version="3", artifacts_bucket="b", memory_id="m")
    with pytest.raises(CM.RuntimeConfigTooLarge, match=r"\d+ chars"):
        CM.serialize_runtime_config(config)
    assert len(json.dumps(config, separators=(",", ":"))) > 4800


def test_bundle_version_null_only_without_applied():
    bootstrap = inputs(bundleArn=None, bundleVersion=None)
    assert CM.check_bootstrap(bootstrap, applied_exists=False) is True
    with pytest.raises(CM.BootstrapNotAllowed):
        CM.check_bootstrap(bootstrap, applied_exists=True)
    assert CM.check_bootstrap(inputs(), applied_exists=True) is False


def test_bundle_arn_mismatch():
    assert CM.check_bundle_arn(None, "arn:b") == "arn:b"
    assert CM.check_bundle_arn("arn:b", "arn:b") == "arn:b"
    with pytest.raises(CM.BundleArnMismatch):
        CM.check_bundle_arn("arn:a", "arn:b")


def test_apply_guard():
    deployer = "arn:aws:sts::111122223333:assumed-role/m11-staging-deployer/m11-operator-op"
    CM.apply_guard("staging", dry_run=True, env={}, caller_arn=OPERATOR)          # previews always work
    CM.apply_guard("dev", dry_run=False, env={}, caller_arn=OPERATOR)             # dev: no guard
    CM.apply_guard("staging", dry_run=False, env={"M11_RELEASE_INPUTS": "/x"}, caller_arn=deployer)
    with pytest.raises(CM.ApplyRefused, match="M11_RELEASE_INPUTS"):
        CM.apply_guard("staging", dry_run=False, env={}, caller_arn=deployer)
    with pytest.raises(CM.ApplyRefused, match="not an m11-prod-deployer session"):
        CM.apply_guard("prod", dry_run=False, env={"M11_RELEASE_INPUTS": "/x"}, caller_arn=deployer)
    with pytest.raises(CM.ApplyRefused):
        CM.apply_guard("prod", dry_run=False, env={"M11_RELEASE_INPUTS": "/x"}, caller_arn=OPERATOR)


def test_adapter_timeout_follows_retained_deadline():
    assert CM.adapter_timeout(inputs()) == 180
    assert CM.adapter_timeout(inputs(retainedPins={"deadlineSeconds": 300})) == 330
