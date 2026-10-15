# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Staging only: AgentCore Gateway `m11-staging-ab` for the pre-staged config-bundle A/B test.

- The Gateway: `authorizer_type="AWS_IAM"`, no protocol type (an AgentCore Runtime target needs one
  without), and its service role `m11-staging-ab-gateway`, which may invoke only the staging endpoint. The
  runtime's resource policy admits that role (program.py).
- One HTTP target `companion` on the staging runtime, qualifier `staging`, signed with the Gateway's role. It is
  the stand-in external surface pinned in `agent/external_surfaces.yaml` (its hash masks ARNs and IDs).
- `m11-staging-abtest-driver`: the only identity granted `bedrock-agentcore:InvokeGateway` on this Gateway, trusted
  by the operators (the A/B traffic driver, `module11 abtest run`).
- `m11-staging-abtest`: the A/B test's execution role (trusted by bedrock-agentcore for `ab-test/*`), reading the
  Gateway, the stage's bundle, the staging online-evaluation config and its results.
The A/B test itself is created with boto3 (Pulumi has no A/B resource), and so is the Gateway's tracing delivery
that attributes scored sessions to a variant (`evaluation/abtest.py`, recorded in the ledger).
Probed on scratch resources: the Gateway ID is `<name>-<10 chars>`, a target with
`GATEWAY_IAM_ROLE` reaches the runtime, and `invoke_agent_runtime` with the target URL as `endpoint_url` works.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pulumi
import pulumi_aws as aws

from module11.infrastructure.platform.roles import role_trust
from module11.release import naming

from .base import Stage
from .evaluation import ONLINE_EVALUATORS

GATEWAY_NAME = "m11-staging-ab"
TARGET_NAME = "companion"


@dataclass
class Gateway:
    outputs: dict = field(default_factory=dict)
    role: aws.iam.Role | None = None
    resource: aws.bedrock.AgentcoreGateway | None = None


def enabled_for(stage: str, flag: bool | None) -> bool:
    """On in staging unless `enableGateway=false`; never elsewhere."""
    return stage == "staging" and flag is not False


def declare(stage: Stage, *, enabled: bool, runtime=None, endpoint_resources: dict | None = None, bundle_arn=None,
            online_eval_arns: dict | None = None) -> Gateway:
    if enabled and stage.name != "staging":
        raise ValueError("the A/B gateway exists in staging only")
    if not enabled:
        return Gateway()
    s = stage.settings
    endpoint_arn = endpoint_resources["staging"].agent_runtime_endpoint_arn
    role_name = naming.role(s.stage, "ab-gateway")          # m11-staging-ab-gateway
    role = stage.role("ab-gateway-role", role_name, "bedrock-agentcore.amazonaws.com",
                      extra_tags={"M11Role": "gateway"}, source_arn=stage.arn("bedrock-agentcore", "gateway/*"))
    policy = stage.role_policy("ab-gateway", role_name, pulumi.Output.all(runtime.agent_runtime_arn, endpoint_arn).apply(
        lambda v: [{"Sid": "InvokeStaging", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
                    "Resource": [v[0], v[1]]}]), opts=pulumi.ResourceOptions(depends_on=[role]))
    gateway = aws.bedrock.AgentcoreGateway(
        "ab-gateway", name=GATEWAY_NAME, authorizer_type="AWS_IAM", role_arn=role.arn,
        description="m11 staging: config-bundle A/B test entry point (design 4.2.9)", tags=stage.tags,
        opts=pulumi.ResourceOptions(depends_on=[policy]))
    target = aws.bedrock.AgentcoreGatewayTarget(
        "ab-target", gateway_identifier=gateway.gateway_id, name=TARGET_NAME,
        description="DevOps Companion staging endpoint (the stand-in external surface, 4.4.19)",
        target_configuration={"http": {"agentcore_runtime": {"arn": runtime.agent_runtime_arn, "qualifier": "staging"}}},
        credential_provider_configuration={"gateway_iam_role": {}})
    driver_name = naming.role(s.stage, "abtest-driver")
    driver = aws.iam.Role("abtest-driver", name=driver_name,
                          assume_role_policy=json.dumps(role_trust(s.operatorPrincipals)),
                          description="Sends the A/B test's sessions through m11-staging-ab (InvokeGateway only)",
                          max_session_duration=4 * 3600, tags={**stage.tags, "M11Role": "abtest-driver"})
    stage.role_policy("abtest-driver", driver_name, gateway.gateway_arn.apply(lambda arn: [
        {"Sid": "InvokeGateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": arn}]),
        opts=pulumi.ResourceOptions(depends_on=[driver]))
    abtest_name = naming.role(s.stage, "abtest")
    abtest = stage.role("abtest-role", abtest_name, "bedrock-agentcore.amazonaws.com",
                        extra_tags={"M11Role": "abtest"}, source_arn=stage.arn("bedrock-agentcore", "ab-test/*"))
    oe_arns = list((online_eval_arns or {}).values())
    stage.role_policy("abtest", abtest_name, pulumi.Output.all(gateway.gateway_arn, bundle_arn, *oe_arns).apply(
        lambda v: [
            {"Sid": "Gateway", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetGateway", "bedrock-agentcore:GetGatewayTarget",
                        "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:CreateGatewayRule",
                        "bedrock-agentcore:UpdateGatewayRule", "bedrock-agentcore:GetGatewayRule",
                        "bedrock-agentcore:DeleteGatewayRule", "bedrock-agentcore:ListGatewayRules"],
             # The Gateway and its rules (the service routes variants with gateway rules it manages).
             "Resource": [v[0], stage.arn("bedrock-agentcore", f"gateway/{GATEWAY_NAME}-*")]},
            {"Sid": "Bundle", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetConfigurationBundle", "bedrock-agentcore:GetConfigurationBundleVersion"],
             "Resource": v[1]},
            *([{"Sid": "OnlineEval", "Effect": "Allow", "Action": "bedrock-agentcore:GetOnlineEvaluationConfig",
                "Resource": list(v[2:])}] if v[2:] else []),
            {"Sid": "Evaluators", "Effect": "Allow", "Action": "bedrock-agentcore:GetEvaluator",
             # The online config's evaluators by exact ID (P1d allows the Builtin.* wildcard for Evaluate only).
             "Resource": [arn for e in ONLINE_EVALUATORS for arn in (stage.arn("bedrock-agentcore", f"evaluator/{e}"),
                                                                       f"arn:aws:bedrock-agentcore:::evaluator/{e}")]},
            {"Sid": "Describe", "Effect": "Allow",
             "Action": ["logs:DescribeLogGroups", "logs:DescribeIndexPolicies", "logs:PutIndexPolicy", "logs:StopQuery"],
             "Resource": "*"},
            {"Sid": "Results", "Effect": "Allow",
             "Action": ["logs:StartQuery", "logs:GetQueryResults", "logs:FilterLogEvents", "logs:GetLogEvents"],
             "Resource": [stage.log_group_arn(f"/aws/bedrock-agentcore/evaluations/results/m11_{stage.key}_oe*"),
                          stage.log_group_arn("aws/spans"), stage.log_group_arn("aws/spans") + ":*"]},
        ]), opts=pulumi.ResourceOptions(depends_on=[abtest]))
    return Gateway({"gatewayArn": gateway.gateway_arn, "gatewayId": gateway.gateway_id,
                    "gatewayUrl": gateway.gateway_url, "gatewayTarget": target.name,
                    "abtestDriverRole": driver.arn, "abtestRole": abtest.arn}, role, gateway)
