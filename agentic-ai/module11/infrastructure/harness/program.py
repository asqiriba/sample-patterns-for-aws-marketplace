# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The m11-harness program body, with its I/O injected.

`run(sources, ...)` declares every resource of one stage. Behavior inputs and code come only from
sealed objects (M7): the release inputs (M11_RELEASE_INPUTS, else `/applied`), `pulumi-inputs.tar`
and the hash-addressed zips; nothing is read from the working copy except this program. `Sources`
is the only I/O, so the Pulumi-mock tests run the same code with in-memory objects.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import pulumi
import pulumi_aws as aws

from module11.infrastructure.platform.roles import role_trust
from module11.release import naming, pins
from module11.release.pulumi_inputs import read_archive

from . import config_model as CM
from . import edge as edge_mod
from . import evaluation, gateway, guardrail, memory, operations, preview_ui, runtime, tools
from .base import Stage

PLACEHOLDER = json.dumps({"state": "none"})


@dataclass
class Sources:
    """Everything the program reads that is not Pulumi config."""
    read_s3: Callable[[str], bytes]
    read_file: Callable[[str], bytes]
    get_applied: Callable[[str], Mapping | None]
    shared_outputs: Callable[[], Mapping]       # /m11/devops-companion/shared/outputs (mr-* stacks)


def _config_getter(config: pulumi.Config) -> Callable[[str], Any]:
    def get(key: str) -> Any:
        raw = config.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return get


def _shared(stage: str, sources: Sources) -> tuple[dict, dict]:
    """(platform, module10) values: stack references for dev/staging/prod, SSM for mr-*."""
    if naming.is_mr(stage):
        doc = dict(sources.shared_outputs())
        return doc, {k: doc[k] for k in ("userPoolId", "userPoolClientId", "stateTable", "browserAuthUrl")}
    platform = pulumi.StackReference("organization/m11-platform/shared")
    module10_ref = pulumi.StackReference(pulumi.Config().get("module10Stack") or "organization/module10-exposure/workshop")
    keys = ("releaseBucket", "imageRepositoryUrl", "imageRepositoryArn", "previewImageRepositoryArn", "evalRunnerRoles",
            "previewDeployerRole", "boundaryArn")
    return ({k: platform.require_output(k) for k in keys},
            {k: module10_ref.require_output(k) for k in ("userPoolId", "userPoolClientId", "stateTable", "browserAuthUrl")})


def run(sources: Sources, *, stack: str, account: str, region: str, caller_arn: str, dry_run: bool,
        env: Mapping[str, str], config: pulumi.Config | None = None) -> dict:
    config = config or pulumi.Config()
    s = CM.settings(_config_getter(config), stack=stack)
    if s.accountId != account:
        raise CM.ConfigError(f"accountId {s.accountId} does not match the caller's account")
    CM.apply_guard(s.stage, dry_run=dry_run, env=env, caller_arn=caller_arn)
    applied = sources.get_applied(s.stage)
    inputs = CM.load_inputs(s.stage, env, sources.read_file, lambda _stage: applied, sources.read_s3)
    bootstrap = CM.check_bootstrap(inputs, applied is not None)
    sealed = read_archive(sources.read_s3(inputs.pulumiInputs["uri"]), inputs.pulumiInputs["sha256"])
    specs = {name.split("/")[1]: doc for name, doc in sealed.items() if name.endswith("/tool.json")}

    platform, module10 = _shared(s.stage, sources)
    release_bucket = naming.RELEASE_BUCKET.format(account=account, region=region)
    boundary = f"arn:aws:iam::{account}:policy/{naming.BOUNDARY}" if s.is_mr else None
    stage = Stage(s, account, region, release_bucket, boundary)

    # 1-3: execution role (trust only), guardrail + retained versions, memory.
    role = runtime.execution_role(stage)
    guard, guard_version, guard_digest = guardrail.declare(stage, sealed["guardrail.yaml"])
    mem = memory.declare(stage)
    # 4-5: the m11-sdk layer and the derived tool Lambdas.
    layer = aws.lambda_.LayerVersion("sdk-layer", layer_name=naming.function(s.stage, "sdk"),
                                     s3_bucket=release_bucket, s3_key=inputs.layer["s3Key"],
                                     compatible_runtimes=["python3.13"], compatible_architectures=["arm64"],
                                     description="m11-sdk: pinned boto3 and numpy (requirements-lambda.txt)")
    retained = inputs.retainedPins or {}
    functions = tools.derive_tools(stage, specs, inputs.handlers, retained.get("toolStatements") or {}, layer)
    own_tool_arns = pulumi.Output.all(**{t: f.qualified_arn for t, f in functions.items()})
    # 5b-6: the runtime role's policy, then the runtime.
    # mr-* runtimes may also pull MR images (m11/devops-companion-preview).
    repositories = (pulumi.Output.all(platform["imageRepositoryArn"], platform["previewImageRepositoryArn"])
                    if s.is_mr else pulumi.Output.all(platform["imageRepositoryArn"]))
    statements = runtime.role_statements(stage, inputs=inputs, image_repositories=repositories,
                                         guardrail_arn=guard.guardrail_arn, own_tool_arns=own_tool_arns)
    role_policy = stage.role_policy("runtime", naming.role(s.stage, "runtime"), statements,
                                    opts=pulumi.ResourceOptions(depends_on=[role]))
    tables = {k: aws.dynamodb.Table(f"table-{k}", name=naming.table(s.stage, k), billing_mode="PAY_PER_REQUEST",
                                    hash_key="pk", range_key="sk",
                                    attributes=[{"name": "pk", "type": "S"}, {"name": "sk", "type": "S"}],
                                    ttl={"attribute_name": "expiresAt", "enabled": True} if k != "control" else None,
                                    tags=stage.tags)
              for k in ("requests", "outcomes", "control")}
    bucket = _artifacts_bucket(stage)
    config_json = pulumi.Output.all(own_tool_arns, guard.guardrail_id, guard_version.version, mem.id).apply(
        lambda v: CM.serialize_runtime_config(CM.runtime_config(
            settings=s, inputs=inputs, tool_arns=v[0], guardrail_id=v[1], guardrail_version=v[2],
            artifacts_bucket=stage.artifacts_bucket, memory_id=v[3])))
    image_uri = pulumi.Output.from_input(platform["imageRepositoryUrl"]).apply(lambda u: f"{u}@{inputs.imageDigest}")
    agent = runtime.declare(stage, inputs=inputs, image_uri=image_uri, role=role, role_policy=role_policy,
                            runtime_config=config_json)
    # 7-9: log groups, endpoints, resource policies (adapter + eval runner only).
    endpoint_resources, _ = runtime.endpoints(stage, agent, role)
    controller_on = operations.enabled(stage, config.get_bool("enableController"))
    queue = operations.shadow_queue(stage, controller=controller_on)
    edge = edge_mod.deploy(stage, inputs=inputs, runtime_arn=agent.agent_runtime_arn, tables=tables, module10=module10,
                           account_logging=s.accessLogs, shadow_queue=queue)
    # 16: the code-based evaluator, before the eval runner, which may Evaluate with it.
    evaluators = evaluation.declare(stage, inputs=inputs, enable_evaluator=config.get_bool("enableEvaluator") is not False)
    eval_role_arn = _eval_runner(stage, platform, agent, endpoint_resources, evaluators)
    # 15: online evaluation configs (dev, staging, prod).
    online_eval_arns = (evaluation.online_eval(stage, runtime=agent)
                        if (s.onlineEval or {}).get("enabled") and not s.is_mr else {})
    # 18: operations (dev, staging, prod; never mr-*): controller, schedule, alarms, and the shadow worker.
    ops = operations.declare(stage, controller=controller_on, drift_checker=False,
                             inputs=inputs, runtime=agent, endpoint_resources=endpoint_resources, tables=tables,
                             sdk_layer=layer, queue=queue)
    # 10: the configuration bundle (container + placeholder initial version).
    bundle, bundle_arn = runtime.bundle(stage, agent, role, inputs.bundleArn)
    # 19: staging only, the A/B Gateway, whose service role the runtime admits.
    gw = gateway.declare(stage, enabled=gateway.enabled_for(s.stage, config.get_bool("enableGateway")), runtime=agent,
                         endpoint_resources=endpoint_resources, bundle_arn=bundle_arn, online_eval_arns=online_eval_arns)
    admitted = [edge.adapter_role.arn, eval_role_arn] + ([ops.shadow_role.arn] if ops.shadow_role else []) + \
        ([gw.role.arn] if gw.role else [])
    runtime.resource_policies(stage, agent, endpoint_resources, admitted,
                              depends_on=[ops.shadow_function] if ops.shadow_function else None)
    # 18b: the drift checker, when the release carries its code (an older release's reapply has none).
    drift_out: dict = {}
    if controller_on and config.get_bool("enableDriftChecker") is not False:
        if ((inputs.extra or {}).get("operationsCode") or {}).get("drift_check"):
            drift_out = operations.drift_checker(stage, ops, runtime=agent, endpoint_resources=endpoint_resources,
                                                 tables=tables, guardrail_arn=guard.guardrail_arn, bundle_arn=bundle_arn)
        else:
            pulumi.log.warn("this release was sealed without operationsCode.drift_check: no drift checker")
    # 14: release-state parameters, values owned by `release deploy`.
    for leaf in ("current", "candidate", "applied", "drift"):
        aws.ssm.Parameter(f"param-{leaf}", name=naming.param(s.stage, leaf), type="String",
                          tier="Advanced" if leaf in ("current", "candidate") else "Standard", value=PLACEHOLDER,
                          tags=stage.tags, opts=pulumi.ResourceOptions(ignore_changes=["value"]))
    # 20: behind a flag, enabled by a later phase.
    preview_ui.declare(stage, enabled=bool(config.get_bool("enableVercel")), api_url=edge.stage.invoke_url,
                       client_id=edge.browser_client.id if edge.browser_client else None,
                       auth_url=module10["browserAuthUrl"], project_id=config.get("vercelProjectId"),
                       team_id=config.get("vercelTeamId"), git_branch=config.get("gitBranch"))

    effective = pulumi.Output.all(own_tool_arns, guard_version.version).apply(lambda v: {
        "granted": pins.granted(pins.Pins.from_dict(retained), v[0], str(v[1])).to_dict(),
        "ownToolArns": dict(sorted(v[0].items())), "guardrailVersion": str(v[1])})
    outputs = {
        "stage": s.stage, "stagekey": s.stagekey, "bootstrap": bootstrap, "manifestVersion": inputs.manifestVersion,
        "runtimeArn": agent.agent_runtime_arn, "runtimeId": agent.agent_runtime_id,
        "runtimeVersion": agent.agent_runtime_version, "runtimeName": naming.runtime_name(s.stage),
        "endpoints": {n: e.agent_runtime_endpoint_arn for n, e in endpoint_resources.items()},
        "toolArns": own_tool_arns, "guardrailId": guard.guardrail_id, "guardrailArn": guard.guardrail_arn,
        "guardrailVersion": guard_version.version, "guardrailDefinitionSha256": guard_digest,
        "bundleArn": bundle_arn, "bundleId": bundle.bundle_id, "memoryId": mem.id, "memoryArn": mem.arn,
        "layerArn": layer.arn, "artifactsBucket": bucket.bucket, "tables": {k: t.name for k, t in tables.items()},
        "apiUrl": edge.stage.invoke_url, "restApiId": edge.api.id, "adapterFunction": edge.adapter.name,
        "adapterRole": edge.adapter_role.arn, "evalRunnerRole": eval_role_arn,
        "browserClientId": edge.browser_client.id if edge.browser_client else None,
        "browserAuthUrl": module10["browserAuthUrl"], "userPoolId": module10["userPoolId"],
        "userPoolClientId": module10["userPoolClientId"], "uiOrigin": s.uiOrigin,
        "teamConcurrency": s.teamConcurrency, "rolloutProfile": s.rolloutProfile, "demoLabels": s.demoLabels,
        "adapterTimeoutSeconds": CM.adapter_timeout(inputs), "evaluatorIds": evaluators.ids,
        "effectivePins": effective, "policyDigests": stage.digests(),
        "controllerFunction": ops.outputs.get("controllerFunction"), "controllerArn": ops.outputs.get("controllerArn"),
        "controllerSchedule": ops.outputs.get("controllerSchedule"), "shadowQueueUrl": ops.outputs.get("shadowQueueUrl"),
        "driftFunction": drift_out.get("driftFunction"), "driftSchedule": drift_out.get("driftSchedule"),
        "onlineEvalConfigs": online_eval_arns, "gateway": gw.outputs or None,
    }
    for key, value in outputs.items():
        pulumi.export(key, value)
    return outputs


def _artifacts_bucket(stage: Stage) -> aws.s3.Bucket:
    """Row 12: `results/` 7 days, `side-effects/` 30 days; versioned (flags restore uses versions)."""
    bucket = aws.s3.Bucket("artifacts", bucket=stage.artifacts_bucket, force_destroy=True, tags=stage.tags)
    aws.s3.BucketPublicAccessBlock("artifacts-private", bucket=bucket.id, block_public_acls=True,
                                   block_public_policy=True, ignore_public_acls=True, restrict_public_buckets=True)
    aws.s3.BucketServerSideEncryptionConfiguration("artifacts-encryption", bucket=bucket.id, rules=[{
        "apply_server_side_encryption_by_default": {"sse_algorithm": "aws:kms"}, "bucket_key_enabled": True}])
    aws.s3.BucketVersioning("artifacts-versioning", bucket=bucket.id, versioning_configuration={"status": "Enabled"})
    aws.s3.BucketLifecycleConfiguration("artifacts-retention", bucket=bucket.id, rules=[
        {"id": "results", "status": "Enabled", "filter": {"prefix": "results/"}, "expiration": {"days": 7},
         "noncurrent_version_expiration": {"noncurrent_days": 7}},
        {"id": "side-effects", "status": "Enabled", "filter": {"prefix": "side-effects/"},
         "expiration": {"days": 30}, "noncurrent_version_expiration": {"noncurrent_days": 30}}])
    aws.s3.BucketPolicy("artifacts-tls", bucket=bucket.id, policy=bucket.arn.apply(lambda arn: json.dumps({
        "Version": "2012-10-17", "Statement": [{"Sid": "TlsOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                                                "Resource": [arn, f"{arn}/*"],
                                                "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]})))
    return bucket


def _eval_runner(stage: Stage, platform: dict, agent, endpoint_resources, evaluators=None) -> pulumi.Output:
    """Row 17. dev/staging/prod: the platform's role gets this stack's policy. mr-*: a role in this
    stack, with the boundary, trusted by the operators and the preview deployer."""
    statements = runtime.eval_runner_statements(stage, agent, endpoint_resources, evaluators)
    name = naming.eval_runner_role(stage.name)
    if not stage.settings.is_mr:
        stage.role_policy("eval-runner", name, statements)
        return pulumi.Output.from_input(platform["evalRunnerRoles"]).apply(lambda r: r[stage.name])
    trust = pulumi.Output.from_input(platform["previewDeployerRoleArn"] if "previewDeployerRoleArn" in platform
                                     else platform["previewDeployerRole"]).apply(
        lambda deployer: json.dumps(role_trust(stage.settings.operatorPrincipals, [deployer])))
    role = aws.iam.Role("eval-runner", name=name, assume_role_policy=trust, permissions_boundary=stage.boundary_arn,
                        max_session_duration=4 * 3600, tags={**stage.tags, "M11Role": "eval-runner"})
    stage.role_policy("eval-runner", name, statements, opts=pulumi.ResourceOptions(depends_on=[role]))
    return role.arn
