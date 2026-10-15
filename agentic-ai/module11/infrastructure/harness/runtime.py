# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Runtime `m11_<stagekey>_companion`, its endpoints, resource policies, log groups and
configuration bundle.

Dependency order, acyclic (M5): execution role (trust only) -> tool roles and Lambdas -> the role's
RolePolicy (invoke list = retainedPins.toolArns plus this stack's tool versions, 4.2.18) -> runtime
(depends_on that policy) -> per-runtime log permissions, endpoint log groups, endpoints, resource
policies -> bundle (its component is keyed by the runtime ARN). The runtime env and the role take
the bundle ARN from `releaseInputs.bundleArn`, a plain value, never from the bundle's Output.
"""
from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws
import pulumi_aws_native as native

from module11.release import naming

from . import config_model as CM
from .base import XRAY, Stage

BOOTSTRAP_COMPONENT = {"configuration": {"schema": "m11.bundle/2", "bootstrap": True}}
LIFECYCLE = [{"idle_runtime_session_timeout": 60, "max_lifetime": 900}]


def execution_role(stage: Stage) -> aws.iam.Role:
    return stage.role("runtime-role", naming.role(stage.name, "runtime"), "bedrock-agentcore.amazonaws.com",
                      extra_tags={"M11Role": "runtime"},
                      source_arn=stage.arn("bedrock-agentcore", "*"))


def role_statements(stage: Stage, *, inputs: CM.ReleaseInputs, image_repositories: pulumi.Input[list],
                    guardrail_arn: pulumi.Output, own_tool_arns: pulumi.Output) -> pulumi.Output:
    retained = inputs.retainedPins or {}
    retained_arns = retained.get("toolArns") or []
    retained_arns = sorted(retained_arns.values()) if isinstance(retained_arns, dict) else list(retained_arns)
    rb, ab = stage.release_bucket, stage.artifacts_bucket

    def render(values):
        guard, own, repos = values
        invoke = sorted(set(retained_arns) | set(own.values()))
        statements = [
            {"Sid": "Images", "Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
             "Resource": sorted(repos)},
            {"Sid": "NoResourceType", "Effect": "Allow",
             "Action": ["ecr:GetAuthorizationToken", "logs:PutResourcePolicy", "logs:DescribeLogGroups",
                        "xray:GetSamplingRules", "xray:GetSamplingTargets"], "Resource": "*"},
            {**XRAY, "Sid": "Traces"},
            # AgentCore sets up the runtime's log groups with this role (CreateLogGroup, then the
            # `otel-rt-logs` and `spans` streams) when the runtime and its endpoints are created. Denied, it
            # creates neither stream: GenAI event records fail with "log stream does not exist" and spans go
            # to `aws/spans` only. The grant must exist before the runtime does, so it names the runtime by
            # its name prefix (the generated ID follows it).
            {"Sid": "RuntimeLogGroups", "Effect": "Allow", "Action": "logs:CreateLogGroup",
             "Resource": stage.arn("logs", f"log-group:/aws/bedrock-agentcore/runtimes/{naming.runtime_name(stage.name)}-*")},
            {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
             "Condition": {"StringLike": {"cloudwatch:namespace": "bedrock-agentcore"}}},
            {"Sid": "InvokeModels", "Effect": "Allow",
             "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
             "Resource": sorted(retained.get("modelResources") or [])},
            {"Sid": "Guardrail", "Effect": "Allow", "Action": "bedrock:ApplyGuardrail", "Resource": guard,
             # mr-* roles state the boundary's tag condition, so the boundary visibly covers them.
             **({"Condition": {"StringEquals": {"aws:ResourceTag/Project": naming.PROJECT_TAG},
                               "StringLike": {"aws:ResourceTag/Stage": "mr-*"}}} if stage.settings.is_mr else {})},
            {"Sid": "InvokeTools", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": invoke},
            {"Sid": "Results", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
             "Resource": stage.s3(ab, "results/*")},
            {"Sid": "Flags", "Effect": "Allow", "Action": "s3:GetObject", "Resource": stage.s3(ab, "config/flags.json")},
            {"Sid": "ReleaseData", "Effect": "Allow", "Action": "s3:GetObject",
             "Resource": [stage.s3(rb, "evidence/*"), stage.s3(rb, "snapshots/policy/*")]},
            {"Sid": "WorkloadIdentity", "Effect": "Allow",
             "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
             "Resource": [stage.arn("bedrock-agentcore", "workload-identity-directory/default"),
                          stage.arn("bedrock-agentcore", "workload-identity-directory/default/workload-identity/"
                                    f"{naming.runtime_name(stage.name)}-*")]},
        ]
        if inputs.bundleArn:
            statements.append({"Sid": "Bundle", "Effect": "Allow",
                               "Action": "bedrock-agentcore:GetConfigurationBundleVersion",
                               "Resource": inputs.bundleArn})
        return [s for s in statements if s["Resource"]]

    return pulumi.Output.all(guardrail_arn, own_tool_arns, image_repositories).apply(render)


def declare(stage: Stage, *, inputs: CM.ReleaseInputs, image_uri: str, role: aws.iam.Role,
            role_policy: aws.iam.RolePolicy, runtime_config: pulumi.Output) -> aws.bedrock.AgentcoreAgentRuntime:
    tags = {**stage.tags, **({"AllowPublicNetwork": "true"} if stage.settings.allowPublicNetwork else {})}
    return aws.bedrock.AgentcoreAgentRuntime(
        "runtime", agent_runtime_name=naming.runtime_name(stage.name),
        description=f"DevOps Companion harness ({stage.name}), released by Module 11",
        agent_runtime_artifact={"container_configuration": {"container_uri": image_uri}},
        role_arn=role.arn, network_configuration={"network_mode": "PUBLIC"},
        protocol_configuration={"server_protocol": "HTTP"}, lifecycle_configurations=LIFECYCLE,
        environment_variables={CM_ENV: runtime_config}, tags=tags,
        opts=pulumi.ResourceOptions(depends_on=[role_policy]))


CM_ENV = "M11_RUNTIME_CONFIG"


def log_group_names(runtime_id: str, endpoints: list[str]) -> list[str]:
    """`/aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>`; -DEFAULT is created by the service
    with the runtime role."""
    return [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{e}" for e in [*endpoints, "DEFAULT"]]


def endpoints(stage: Stage, runtime: aws.bedrock.AgentcoreAgentRuntime, role: aws.iam.Role
              ) -> tuple[dict[str, aws.bedrock.AgentcoreAgentRuntimeEndpoint], aws.iam.RolePolicy]:
    """Pre-created 30-day log groups (adopted for named endpoints), the role's log permissions on
    those literal groups, then the endpoints (version owned by `release deploy`: ignore_changes).
    `logs:CreateLogGroup` is in the main runtime policy (RuntimeLogGroups), which exists before the
    runtime; the service-created -DEFAULT group gets its retention after deploy and is deleted by
    `release destroy`."""
    names = stage.settings.endpoints
    groups = [stage.log_group(f"runtime-logs-{e}", runtime.agent_runtime_id.apply(
        lambda rid, e=e: f"/aws/bedrock-agentcore/runtimes/{rid}-{e}")) for e in names]
    logs_policy = stage.role_policy("runtime-logs", naming.role(stage.name, "runtime"),
                                    runtime.agent_runtime_id.apply(lambda rid: [
                                        {"Sid": "RuntimeLogs", "Effect": "Allow",
                                         "Action": ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                                         "Resource": [stage.log_group_arn(g) + ":*" for g in log_group_names(rid, names)]}]),
                                    opts=pulumi.ResourceOptions(depends_on=[role, runtime]))
    out = {}
    for name, group in zip(names, groups):
        out[name] = aws.bedrock.AgentcoreAgentRuntimeEndpoint(
            f"endpoint-{name}", agent_runtime_id=runtime.agent_runtime_id, name=name,
            agent_runtime_version=runtime.agent_runtime_version,
            description=f"{stage.name} {name} endpoint (version retargeted by release deploy)", tags=stage.tags,
            opts=pulumi.ResourceOptions(depends_on=[group, logs_policy], ignore_changes=["agentRuntimeVersion"]))
    return out, logs_policy


def resource_policies(stage: Stage, runtime: aws.bedrock.AgentcoreAgentRuntime,
                      endpoint_resources: dict[str, aws.bedrock.AgentcoreAgentRuntimeEndpoint],
                      principals: list[pulumi.Output], depends_on: list | None = None) -> None:
    """Allow exactly the listed principals; Deny InvokeAgentRuntime to everyone else.
    `depends_on` orders the policies after resources that prove a new principal has propagated (a role
    created in the same update is rejected as "Invalid principal in policy" for a few seconds)."""
    targets = [("runtime", runtime.agent_runtime_arn)] + [
        (f"endpoint-{n}", e.agent_runtime_endpoint_arn) for n, e in endpoint_resources.items()]
    for logical, arn in targets:
        document = pulumi.Output.all(arn, *principals).apply(lambda v: json.dumps({
            "Version": "2012-10-17", "Statement": [
                {"Sid": "Admitted", "Effect": "Allow", "Principal": {"AWS": sorted(v[1:])},
                 "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
                 "Resource": v[0]},
                {"Sid": "EveryoneElse", "Effect": "Deny", "Principal": "*",
                 "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": v[0],
                 "Condition": {"ArnNotEquals": {"aws:PrincipalArn": sorted(v[1:])}}}]}, sort_keys=True))
        aws.bedrock.AgentcoreResourcePolicy(f"{logical}-admission", resource_arn=arn, policy=document,
                                            opts=pulumi.ResourceOptions(depends_on=list(endpoint_resources.values())
                                                                        + list(depends_on or [])))


def bundle(stage: Stage, runtime: aws.bedrock.AgentcoreAgentRuntime, role: aws.iam.Role,
           expected_arn: str | None) -> tuple[native.bedrockagentcore.ConfigurationBundle, pulumi.Output]:
    """The bundle container with the fixed placeholder component (its initial version, 4.2.4)."""
    resource = native.bedrockagentcore.ConfigurationBundle(
        "bundle", bundle_name=naming.bundle_name(stage.name),
        description=f"DevOps Companion configuration ({stage.name}); versions created by release deploy",
        branch_name=stage.name, commit_message="bootstrap placeholder (m11-harness)",
        created_by={"name": "m11-harness", "arn": role.arn},
        components=runtime.agent_runtime_arn.apply(lambda arn: {arn: BOOTSTRAP_COMPONENT}),
        tags=[{"key": k, "value": v} for k, v in stage.tags.items()],
        opts=pulumi.ResourceOptions(ignore_changes=["components", "commitMessage", "branchName"]))
    checked = resource.bundle_arn.apply(lambda actual: CM.check_bundle_arn(expected_arn, actual))
    return resource, checked


def eval_runner_statements(stage: Stage, runtime: aws.bedrock.AgentcoreAgentRuntime,
                           endpoint_resources: dict[str, aws.bedrock.AgentcoreAgentRuntimeEndpoint],
                           evaluators=None) -> pulumi.Output:
    """Row 17. `Evaluate` covers the built-in evaluators and this stack's own evaluator ARNs. The role may also invoke
    the code-based evaluator's published version: `Evaluate` calls that Lambda with the CALLER's credentials (settled
    live)."""
    names = stage.settings.endpoints
    evaluator_arns = list(getattr(evaluators, "arns", []) or [])
    function_arns = list(getattr(evaluators, "function_arns", []) or [])
    count, fcount = len(evaluator_arns), len(function_arns)
    return pulumi.Output.all(runtime.agent_runtime_arn, runtime.agent_runtime_id, *evaluator_arns, *function_arns,
                             *[e.agent_runtime_endpoint_arn for e in endpoint_resources.values()]).apply(
        lambda v: [
            {"Sid": "Invoke", "Effect": "Allow",
             "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
             "Resource": [v[0], *v[2 + count + fcount:]]},
            {"Sid": "Evaluate", "Effect": "Allow", "Action": "bedrock-agentcore:Evaluate",
             # IAM authorizes a built-in evaluator as <region>:<account>:evaluator/Builtin.X (live), not as
             # the account-less ARN the API reference shows; both forms are listed.
             "Resource": [stage.arn("bedrock-agentcore", "evaluator/Builtin.*"),
                          "arn:aws:bedrock-agentcore:::evaluator/Builtin.*", *sorted(v[2:2 + count])]},
            *([{"Sid": "InvokeEvaluator", "Effect": "Allow", "Action": "lambda:InvokeFunction",
                "Resource": sorted(v[2 + count:2 + count + fcount])}] if fcount else []),
            {"Sid": "Spans", "Effect": "Allow",
             "Action": ["logs:FilterLogEvents", "logs:StartQuery", "logs:GetQueryResults"],
             "Resource": [stage.log_group_arn("aws/spans") + ":*"] +
                         [stage.log_group_arn(g) + ":*" for g in log_group_names(v[1], names)]},
        ])
