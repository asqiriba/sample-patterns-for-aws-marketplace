# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pulumi-mock cases of the m11-harness program, shared by
tests/test_module11_pulumi_inputs.py (no tests here; the name keeps it in the seeded test set). `execute(stage, ...)` runs program.run under pulumi mocks with
in-memory sealed objects and returns (outputs, registered resources)."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

pulumi = pytest.importorskip("pulumi")
pytest.importorskip("pulumi_aws")
pytest.importorskip("pulumi_aws_native")

from module11.release import pulumi_inputs as PI  # noqa: E402
from module11.release.hashing import sha256_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"
A, R = "111122223333", "us-east-1"
SHA = "sha256:" + "c" * 64
TOOLS = ("check_rollback_readiness", "get_blast_radius", "get_deployment_history", "get_service_logs", "query_metrics",
         "record_incident_note", "search_runbooks")
ARCHIVE = PI.build_archive(FIXTURE)
REPO_URL = f"{A}.dkr.ecr.{R}.amazonaws.com/m11/devops-companion"
SHARED = {"releaseBucket": f"m11-release-{A}-{R}", "imageRepositoryUrl": REPO_URL,
          "imageRepositoryArn": f"arn:aws:ecr:{R}:{A}:repository/m11/devops-companion",
          "previewImageRepositoryArn": f"arn:aws:ecr:{R}:{A}:repository/m11/devops-companion-preview",
          "previewDeployerRoleArn": f"arn:aws:iam::{A}:role/m11-preview-deployer",
          "previewDeployerRole": f"arn:aws:iam::{A}:role/m11-preview-deployer",
          "boundaryArn": f"arn:aws:iam::{A}:policy/m11-mr-boundary",
          "evalRunnerRoles": {s: f"arn:aws:iam::{A}:role/m11-{s}-eval-runner" for s in ("dev", "staging", "prod")},
          "userPoolId": "us-east-1_EXAMPLE", "userPoolClientId": "m10client", "stateTable": "request-state-abc",
          "browserAuthUrl": "https://auth.example.com"}


def tool_arn(stage, tool, version):
    return f"arn:aws:lambda:{R}:{A}:function:m11-{stage}-{tool}:{version}"


def inputs(stage="mr-7", **over):
    doc = {"imageDigest": "sha256:" + "d" * 64, "bundleArn": f"arn:aws:bedrock-agentcore:{R}:{A}:configuration-bundle/"
           f"m11_{stage.replace('-', '')}_companion_cfg-MOCK", "bundleVersion": "v-2",
           "bundleComponent": {"uri": "s3://r/c.json", "sha256": SHA},
           "handlers": {t: {"s3Key": f"handlers/{t}/{'e' * 64}.zip", "codeSha256": base64.b64encode(
               hashlib.sha256(t.encode()).digest()).decode()} for t in TOOLS},
           "layer": {"s3Key": "layers/m11-sdk/x.zip", "sha256": SHA}, "edge": {"s3Key": "handlers/edge/x.zip", "sha256": SHA},
           "evaluatorCode": {"s3Key": "handlers/eval/x.zip", "codeSha256": "x="}, "flagsSha256": SHA,
           "operationsCode": {"layer": {"s3Key": "layers/m11-ops/x.zip", "sha256": SHA},
                              "controller": {"s3Key": "handlers/ops-controller/x.zip", "codeSha256": "c="},
                              "shadow_worker": {"s3Key": "handlers/ops-shadow-worker/x.zip", "codeSha256": "w="},
                              "drift_check": {"s3Key": "handlers/ops-drift-check/x.zip", "codeSha256": "d="}},
           "snapshot": {"id": "pol-1", "uri": f"s3://m11-release-{A}-{R}/snapshots/policy/pol-1.json", "sha256": SHA},
           "evidence": {"uri": f"s3://m11-release-{A}-{R}/evidence/aa/", "sha256": SHA},
           "pulumiInputs": {"uri": "s3://r/releases/mv/pulumi-inputs.tar", "sha256": sha256_bytes(ARCHIVE)},
           "retainedPins": {"toolArns": [tool_arn(stage, "search_runbooks", 4)],
                            "modelResources": ["arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-luna",
                                               f"arn:aws:bedrock:{R}:{A}:inference-profile/us.openai.gpt-5.6-luna"],
                            "deadlineSeconds": 150,
                            "toolStatements": {"search_runbooks": [{"Effect": "Allow", "Action": ["s3:GetObject"],
                                                                    "Resource": [f"arn:aws:s3:::m11-release-{A}-{R}/snapshots/policy/*"]}]}},
           "runtimeDigest": "sha256:" + "f" * 64, "manifestVersion": "2026-10-06T10:00:00Z/abcdef1",
           "behaviorDigest": "sha256:" + "a" * 64}
    doc.update(over)
    return doc


class Mocks(pulumi.runtime.Mocks):
    def __init__(self, bundle_arn=None):
        self.resources = []
        self.outputs = []            # (type, name, outputs): what stack policies see
        self.bundle_arn = bundle_arn

    def new_resource(self, args):
        t, name, inp = args.typ, args.name, dict(args.inputs)
        if t == "pulumi:pulumi:StackReference":
            return f"{name}-id", {"name": inp.get("name"), "outputs": dict(SHARED)}
        self.resources.append((t, name, inp))
        out = dict(inp)
        short = t.rsplit(":", 1)[-1]
        if short == "Role":
            out["arn"] = f"arn:aws:iam::{A}:role/{inp['name']}"
        elif short == "Function":
            out.update(arn=f"arn:aws:lambda:{R}:{A}:function:{inp['name']}", version="1",
                       qualifiedArn=f"arn:aws:lambda:{R}:{A}:function:{inp['name']}:1",
                       invokeArn=f"arn:aws:apigateway:{R}:lambda:path/functions/{inp['name']}/invocations")
        elif short == "LayerVersion":
            out["arn"] = f"arn:aws:lambda:{R}:{A}:layer:{inp['layerName']}:1"
        elif short == "Guardrail":
            out.update(guardrailArn=f"arn:aws:bedrock:{R}:{A}:guardrail/gmock", guardrailId="gmock")
        elif short == "GuardrailVersion":
            out["version"] = "1"
        elif short == "AgentcoreMemory":
            out["arn"] = f"arn:aws:bedrock-agentcore:{R}:{A}:memory/{inp['name']}-MOCK"
        elif short == "AgentcoreAgentRuntime":
            rid = f"{inp['agentRuntimeName']}-MOCK"
            out.update(agentRuntimeArn=f"arn:aws:bedrock-agentcore:{R}:{A}:runtime/{rid}", agentRuntimeId=rid,
                       agentRuntimeVersion="1")
        elif short == "AgentcoreAgentRuntimeEndpoint":
            out["agentRuntimeEndpointArn"] = (f"arn:aws:bedrock-agentcore:{R}:{A}:runtime/{inp['agentRuntimeId']}"
                                             f"/runtime-endpoint/{inp['name']}")
        elif short == "ConfigurationBundle":
            out.update(bundleArn=self.bundle_arn or f"arn:aws:bedrock-agentcore:{R}:{A}:configuration-bundle/"
                       f"{inp['bundleName']}-MOCK", bundleId=f"{inp['bundleName']}-MOCK", versionId="v-initial")
        elif short == "AgentcoreEvaluator":
            eid = f"{inp['evaluatorName']}-MOCK123456"
            out.update(evaluatorId=eid, evaluatorArn=f"arn:aws:bedrock-agentcore:{R}:{A}:evaluator/{eid}")
        elif short == "Table":
            out["arn"] = f"arn:aws:dynamodb:{R}:{A}:table/{inp['name']}"
        elif short == "Bucket":
            out["arn"] = f"arn:aws:s3:::{inp['bucket']}"
        elif short == "RestApi":
            out.update(executionArn=f"arn:aws:execute-api:{R}:{A}:apiid", rootResourceId="root")
        elif short == "Stage":
            out["invokeUrl"] = f"https://apiid.execute-api.{R}.amazonaws.com/{inp.get('stageName')}"
        elif short == "ApiKey":
            out["value"] = "mock-key-value"
        elif short == "LogGroup":
            out["arn"] = f"arn:aws:logs:{R}:{A}:log-group:{inp['name']}"
        elif short == "Queue":
            out.update(arn=f"arn:aws:sqs:{R}:{A}:{inp['name']}", url=f"https://sqs.{R}.amazonaws.com/{A}/{inp['name']}")
        elif short == "AgentcoreOnlineEvaluationConfig":
            cid = f"{inp['onlineEvaluationConfigName']}-MOCK123456"
            out.update(onlineEvaluationConfigId=cid,
                       onlineEvaluationConfigArn=f"arn:aws:bedrock-agentcore:{R}:{A}:online-evaluation-config/{cid}")
        elif short == "AgentcoreGateway":
            gid = f"{inp['name']}-mock123456"
            out.update(gatewayId=gid, gatewayArn=f"arn:aws:bedrock-agentcore:{R}:{A}:gateway/{gid}",
                       gatewayUrl=f"https://{gid}.gateway.bedrock-agentcore.{R}.amazonaws.com")
        elif short == "AgentcoreGatewayTarget":
            out["targetId"] = "MOCKTARGET"
        elif short == "Schedule":
            out["arn"] = f"arn:aws:scheduler:{R}:{A}:schedule/{inp.get('groupName', 'default')}/{inp['name']}"
        self.outputs.append((t, name, out))
        return f"{name}-id", out

    def call(self, args):
        return {}, None

    def of(self, short):
        return [(n, i) for t, n, i in self.resources if t.rsplit(":", 1)[-1] == short]


def execute(stage="mr-7", doc=None, *, bundle_arn=None, applied=None, config=None, operators=("arn:aws:iam::111122223333:user/op",)):
    from module11.infrastructure.harness import program
    mocks = Mocks(bundle_arn)
    pulumi.runtime.set_mocks(mocks, project="m11-harness", stack=stage, preview=False)
    values = {"stage": stage, "accountId": A, "region": R, "operatorPrincipals": json.dumps(list(operators)),
              **({"uiOrigin": "https://ui.example.com"} if stage in ("prod",) or stage.startswith("mr-") else {}),
              **({"uiPreview": "pending"} if stage.startswith("mr-") else {}), **(config or {})}
    pulumi.runtime.set_all_config({f"m11-harness:{k}": str(v) for k, v in values.items()})
    doc = doc or inputs(stage)
    holder = {}

    @pulumi.runtime.test
    def body():
        sources = program.Sources(read_s3=lambda uri: ARCHIVE, read_file=lambda path: json.dumps(doc).encode(),
                                  get_applied=lambda s: applied, shared_outputs=lambda: SHARED)
        outs = program.run(sources, stack=stage, account=A, region=R, caller_arn=f"arn:aws:iam::{A}:user/op",
                           dry_run=False, env={"M11_RELEASE_INPUTS": "/release-inputs.json"})
        keys = list(outs)
        return pulumi.Output.all(*[pulumi.Output.from_input(outs[k]) for k in keys]).apply(
            lambda vals: holder.update(dict(zip(keys, vals))))
    body()
    return holder, mocks


def policy(inputs_):
    return json.loads(inputs_["policy"])
