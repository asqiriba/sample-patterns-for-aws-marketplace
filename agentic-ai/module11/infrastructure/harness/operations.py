# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Operations resources: dev, staging and prod only. An `mr-*`
stack never creates any of them; there `release deploy` is the only writer of its control item.

- The rollback controller `m11-<stage>-controller` (Python 3.13, the m11-sdk layer plus the m11-ops layer
  for PyYAML; code from the sealed `operationsCode.controller` zip): reserved concurrency 1, timeout 50 s,
  published (P2), async event config max age 60 s / 0 retries. It is the only writer of
  `{pk: ROLLOUT, sk: CURRENT}`; `release deploy` and `module11 rollout tick` invoke it.
- Its every-minute schedule `m11-<stage>-controller` (`cron(* * * * ? *)`), created DISABLED with
  `ignore_changes=["state"]`; only the controller's `open` enables it (GetSchedule -> UpdateSchedule), and a
  terminal transition disables it. Retry policy: max event age 60 s, 0 retries. The scheduler's own
  role may invoke only the controller's published version.
- Alarms `m11-<stage>-rollback-stuck`, `-rollout-stalled` and `-ledger-write-failed` on the controller's
  m11/Controller metrics (Stage dimension).
- With `shadow` (dev and prod): SQS `m11-<stage>-shadow` (the adapter enqueues while shadowPct > 0) and the
  shadow worker `m11-<stage>-shadow-worker` (reserved concurrency 5) consuming it.
Every Lambda here gets a pre-created 30-day `/aws/lambda/<function>` log group.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pulumi
import pulumi_aws as aws

from module11.release import naming

from . import config_model as CM
from .base import Stage, lambda_logs_statement

CONTROLLER_TIMEOUT = 50
SHADOW_WORKER_TIMEOUT = 240           # deadlineSeconds (150) + slack for the result read and the outcome write
SHADOW_WORKER_CONCURRENCY = 5
METRIC_NAMESPACE = "m11/Controller"
ALARMS = {"rollback-stuck": "RollbackStuck", "rollout-stalled": "RolloutStalled",
          "ledger-write-failed": "LedgerWriteFailures"}
CONTROLLER_HANDLER = "module11.lambdas.controller.handler.handler"
BATCH_RESULTS_GROUP = "/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default"


def _shadow_verdict_statements(stage: Stage, runtime_id: str) -> list[dict]:
    """The controller's shadow-verdict grants, on stages with a shadow phase:
    StartBatchEvaluation (no resource type), Get and Tag on this stage's `m11_<stagekey>_shadow*` jobs, and log
    reads on aws/spans, the runtime's stable and candidate endpoint groups, and the batch result group."""
    groups = ["aws/spans", f"/aws/bedrock-agentcore/runtimes/{runtime_id}-stable",
              f"/aws/bedrock-agentcore/runtimes/{runtime_id}-candidate", BATCH_RESULTS_GROUP]
    # Measured 2026-10-08 with a federated session holding only these grants: the service acts with
    # forward access sessions (FAS) on the caller's permissions. Without DescribeLogGroups StartBatchEvaluation
    # is refused; without create/retention/stream on the result group it is denied; without Evaluate on the
    # built-in evaluators every session fails.
    return [
        {"Sid": "ShadowBatchStart", "Effect": "Allow", "Action": "bedrock-agentcore:StartBatchEvaluation",
         "Resource": "*"},
        {"Sid": "ShadowBatchJobs", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetBatchEvaluation", "bedrock-agentcore:TagResource"],
         "Resource": stage.arn("bedrock-agentcore", f"batch-evaluate/m11_{stage.key}_shadow*")},
        {"Sid": "ShadowBatchEvaluators", "Effect": "Allow", "Action": "bedrock-agentcore:Evaluate",
         "Resource": [stage.arn("bedrock-agentcore", "evaluator/Builtin.*"),
                      "arn:aws:bedrock-agentcore:::evaluator/Builtin.*"]},
        {"Sid": "ShadowBatchLogs", "Effect": "Allow",
         "Action": ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:StartQuery", "logs:GetQueryResults"],
         "Resource": [stage.log_group_arn(g) + ":*" for g in groups]},
        {"Sid": "ShadowBatchDescribe", "Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": "*"},
        {"Sid": "ShadowBatchResults", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:PutRetentionPolicy", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": [stage.log_group_arn(BATCH_RESULTS_GROUP), stage.log_group_arn(BATCH_RESULTS_GROUP) + ":*"]},
    ]
SHADOW_HANDLER = "module11.lambdas.shadow_worker.handler.handler"
DRIFT_HANDLER = "module11.lambdas.drift_check.handler.handler"
DRIFT_TIMEOUT = 120
DRIFT_SCHEDULE = "rate(15 minutes)"
DRIFT_NAMESPACE = "m11/Drift"


@dataclass
class Operations:
    outputs: dict = field(default_factory=dict)
    shadow_queue: aws.sqs.Queue | None = None
    shadow_role: aws.iam.Role | None = None
    shadow_function: aws.lambda_.Function | None = None     # created after its role has propagated
    scheduler_role: aws.iam.Role | None = None
    code: dict | None = None
    layers: list | None = None


def enabled(stage: Stage, flag: bool | None) -> bool:
    """dev, staging and prod create the controller unless `enableController=false`; mr-* never."""
    return not stage.settings.is_mr and flag is not False


def shadow_queue(stage: Stage, *, controller: bool) -> aws.sqs.Queue | None:
    """Created before the edge, which enqueues to it (dev and prod: `shadow` in the stage settings)."""
    if not controller or not stage.settings.shadow:
        return None
    return aws.sqs.Queue("shadow-queue", name=naming.function(stage.name, "shadow"),
                         visibility_timeout_seconds=SHADOW_WORKER_TIMEOUT * 6, message_retention_seconds=86400,
                         sqs_managed_sse_enabled=True, tags=stage.tags)


def schedule_name(stage: str) -> str:
    return naming.function(stage, "controller")


def declare(stage: Stage, *, controller: bool, drift_checker: bool, inputs: CM.ReleaseInputs | None = None,
            runtime=None, endpoint_resources: dict | None = None, tables: dict | None = None,
            sdk_layer=None, queue: aws.sqs.Queue | None = None) -> Operations:
    if stage.settings.is_mr and (controller or drift_checker or queue is not None):
        raise ValueError("mr-* stacks create no controller, schedule or drift checker")
    if not controller:
        return Operations()
    code = (inputs.extra or {}).get("operationsCode") if inputs else None
    if not code:
        raise CM.ConfigError("this release was sealed before the controller existed (no operationsCode); "
                             "re-seal it or deploy a newer release")
    s = stage.settings
    ops_layer = aws.lambda_.LayerVersion("ops-layer", layer_name=naming.function(s.stage, "ops"),
                                         s3_bucket=stage.release_bucket, s3_key=code["layer"]["s3Key"],
                                         compatible_runtimes=["python3.13"], compatible_architectures=["arm64"],
                                         description="m11-ops: PyYAML for the release engines (lambdas/requirements.txt)")
    layers = [sdk_layer.arn, ops_layer.arn]
    endpoints = list(s.endpoints)
    runtime_arn, runtime_id = runtime.agent_runtime_arn, runtime.agent_runtime_id
    control_arn, outcomes_arn = tables["control"].arn, tables["outcomes"].arn
    params = [stage.arn("ssm", f"parameter{naming.param(s.stage, leaf)}") for leaf in ("current", "candidate")]
    latches = stage.arn("ssm", f"parameter{naming.PARAM_ROOT}/latch/*")
    sched = schedule_name(s.stage)
    schedule_arn = stage.arn("scheduler", f"schedule/default/{sched}")

    # The scheduler's role (trust only here; its invoke policy names the controller version below).
    scheduler_role_name = naming.role(s.stage, "scheduler")
    # Scheduler validates the trust with the schedule GROUP's ARN as aws:SourceArn (live: a schedule ARN is
    # rejected with "The execution role you provide must allow AWS EventBridge Scheduler to assume the role").
    scheduler_role = stage.role("scheduler-role", scheduler_role_name, "scheduler.amazonaws.com",
                                extra_tags={"M11Role": "scheduler"},
                                source_arn=stage.arn("scheduler", "schedule-group/default"))

    name = naming.function(s.stage, "controller")
    role = stage.role("controller-role", naming.role(s.stage, "controller"), "lambda.amazonaws.com",
                      extra_tags={"M11Role": "controller"})
    logs = stage.log_group("controller-logs", f"/aws/lambda/{name}")
    statements = pulumi.Output.all(control_arn, outcomes_arn, runtime_arn, scheduler_role.arn, runtime_id).apply(lambda v: [
        *(_shadow_verdict_statements(stage, v[4]) if s.shadow else []),
        lambda_logs_statement(stage, name),
        {"Sid": "ControlItem", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"],
         "Resource": v[0]},
        {"Sid": "Outcomes", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:Query"], "Resource": v[1]},
        {"Sid": "Endpoints", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetAgentRuntimeEndpoint", "bedrock-agentcore:UpdateAgentRuntimeEndpoint"],
         "Resource": [v[2]] + [f"{v[2]}/runtime-endpoint/{e}" for e in endpoints]},
        {"Sid": "Schedule", "Effect": "Allow", "Action": ["scheduler:GetSchedule", "scheduler:UpdateSchedule"],
         "Resource": schedule_arn},
        {"Sid": "PassSchedulerRole", "Effect": "Allow", "Action": "iam:PassRole", "Resource": v[3],
         "Condition": {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}},
        {"Sid": "ReleaseState", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
         "Resource": params},
        {"Sid": "Latches", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"], "Resource": latches},
        {"Sid": "Ledger", "Effect": "Allow", "Action": "s3:PutObject",
         "Resource": stage.s3(stage.release_bucket, f"ledger/{stage.key}/*")},
        {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}}},
    ])
    policy = stage.role_policy("controller", naming.role(s.stage, "controller"), statements,
                               opts=pulumi.ResourceOptions(depends_on=[role]))
    fn = aws.lambda_.Function(
        "controller", name=name, role=role.arn, runtime="python3.13", architectures=["arm64"],
        handler=CONTROLLER_HANDLER, s3_bucket=stage.release_bucket, s3_key=code["controller"]["s3Key"],
        timeout=CONTROLLER_TIMEOUT, memory_size=512, layers=layers, publish=True, reserved_concurrent_executions=1,
        environment={"variables": {
            "M11_STAGE": s.stage, "CONTROL_TABLE": tables["control"].name, "OUTCOMES_TABLE": tables["outcomes"].name,
            "RUNTIME_ID": runtime_id, "SCHEDULE_NAME": sched, "SCHEDULE_GROUP": "default",
            "CURRENT_PARAM": naming.param(s.stage, "current"), "CANDIDATE_PARAM": naming.param(s.stage, "candidate"),
            "RELEASE_BUCKET": stage.release_bucket}},
        tags=stage.tags, opts=pulumi.ResourceOptions(depends_on=[policy, logs]))
    aws.lambda_.FunctionEventInvokeConfig("controller-async", function_name=fn.name, qualifier=fn.version,
                                          maximum_event_age_in_seconds=60, maximum_retry_attempts=0)
    scheduler_policy = stage.role_policy("scheduler", scheduler_role_name, fn.qualified_arn.apply(lambda arn: [
        {"Sid": "InvokeController", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": arn}]),
        opts=pulumi.ResourceOptions(depends_on=[scheduler_role]))
    schedule = aws.scheduler.Schedule(
        "controller-schedule", name=sched, group_name="default", schedule_expression="cron(* * * * ? *)",
        description=f"m11 {s.stage} rollback controller tick (enabled only while a rollout is active)",
        state="DISABLED", flexible_time_window={"mode": "OFF"},
        target={"arn": fn.qualified_arn, "role_arn": scheduler_role.arn, "input": json.dumps({"command": "tick"}),
                "retry_policy": {"maximum_event_age_in_seconds": 60, "maximum_retry_attempts": 0}},
        opts=pulumi.ResourceOptions(depends_on=[scheduler_policy], ignore_changes=["state"]))
    for short, metric in ALARMS.items():
        aws.cloudwatch.MetricAlarm(
            f"alarm-{short}", name=naming.function(s.stage, short), namespace=METRIC_NAMESPACE, metric_name=metric,
            dimensions={"Stage": s.stage}, statistic="Sum", period=60, evaluation_periods=1, threshold=1,
            comparison_operator="GreaterThanOrEqualToThreshold", treat_missing_data="notBreaching",
            alarm_description=f"m11 {s.stage}: {short.replace('-', ' ')} (design 4.4.8)", tags=stage.tags)
    out = {"controllerFunction": fn.name, "controllerArn": fn.qualified_arn, "controllerSchedule": schedule.name,
           "scheduleArn": schedule.arn}
    ops = Operations(out, scheduler_role=scheduler_role, code=code, layers=layers)
    if queue is not None:
        ops.shadow_queue = queue
        ops.shadow_role, ops.shadow_function = _shadow_worker(stage, code, layers, queue, runtime_arn, control_arn,
                                                              outcomes_arn)
        out["shadowQueueUrl"] = queue.url
    return ops


def drift_checker(stage: Stage, ops: Operations, *, runtime, endpoint_resources: dict, tables: dict, guardrail_arn,
                  bundle_arn) -> dict:
    """`m11-<stage>-drift-check` and its `rate(15 minutes)` schedule, created ENABLED. Read-only
    except `/drift` and the m11/Drift metric. Needs a release sealed with `operationsCode.drift_check`."""
    code = (ops.code or {}).get("drift_check")
    if not code:
        raise CM.ConfigError("this release was sealed before the drift checker existed (no operationsCode.drift_check); "
                             "re-seal it or deploy with enableDriftChecker=false")
    s = stage.settings
    name = naming.function(s.stage, "drift-check")
    role_name = naming.role(s.stage, "drift-check")
    role = stage.role("drift-check-role", role_name, "lambda.amazonaws.com", extra_tags={"M11Role": "drift-checker"})
    logs = stage.log_group("drift-check-logs", f"/aws/lambda/{name}")
    params = [stage.arn("ssm", f"parameter{naming.param(s.stage, leaf)}") for leaf in ("current", "candidate", "applied")]
    drift_param = stage.arn("ssm", f"parameter{naming.param(s.stage, 'drift')}")
    endpoints = list(s.endpoints)
    rb = stage.release_bucket
    statements = pulumi.Output.all(runtime.agent_runtime_arn, tables["control"].arn, guardrail_arn, bundle_arn).apply(lambda v: [
        lambda_logs_statement(stage, name),
        {"Sid": "ReleaseState", "Effect": "Allow", "Action": "ssm:GetParameter", "Resource": params},
        {"Sid": "DriftReport", "Effect": "Allow", "Action": "ssm:PutParameter", "Resource": drift_param},
        {"Sid": "ControlItem", "Effect": "Allow", "Action": "dynamodb:GetItem", "Resource": v[1]},
        {"Sid": "Pins", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": [stage.s3(rb, f"release-inputs/{stage.key}/*"), stage.s3(rb, f"ledger/{stage.key}/*")]},
        {"Sid": "LedgerList", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": stage.s3(rb),
         "Condition": {"StringLike": {"s3:prefix": f"ledger/{stage.key}/*"}}},
        {"Sid": "Flags", "Effect": "Allow", "Action": "s3:GetObject",
         "Resource": stage.s3(stage.artifacts_bucket, "config/flags.json")},
        {"Sid": "Runtime", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:GetAgentRuntimeEndpoint"],
         "Resource": [v[0]] + [f"{v[0]}/runtime-endpoint/{e}" for e in endpoints]},
        {"Sid": "BundleVersion", "Effect": "Allow", "Action": "bedrock-agentcore:GetConfigurationBundleVersion",
         "Resource": v[3]},
        # The service authorizes ListConfigurationBundleVersions against configuration-bundle/* (probed live).
        {"Sid": "BundleVersions", "Effect": "Allow", "Action": "bedrock-agentcore:ListConfigurationBundleVersions",
         "Resource": stage.arn("bedrock-agentcore", "configuration-bundle/*")},
        {"Sid": "Tools", "Effect": "Allow", "Action": "lambda:GetFunctionConfiguration",
         "Resource": stage.arn("lambda", f"function:m11-{stage.key}-*")},
        {"Sid": "RolePolicies", "Effect": "Allow", "Action": "iam:GetRolePolicy",
         "Resource": f"arn:{stage.partition}:iam::{stage.account}:role/m11-{stage.key}-*"},
        {"Sid": "Guardrail", "Effect": "Allow", "Action": "bedrock:GetGuardrail", "Resource": v[2]},
        {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
         "Condition": {"StringEquals": {"cloudwatch:namespace": DRIFT_NAMESPACE}}},
    ])
    policy = stage.role_policy("drift-check", role_name, statements, opts=pulumi.ResourceOptions(depends_on=[role]))
    fn = aws.lambda_.Function(
        "drift-check", name=name, role=role.arn, runtime="python3.13", architectures=["arm64"],
        handler=DRIFT_HANDLER, s3_bucket=stage.release_bucket, s3_key=code["s3Key"], timeout=DRIFT_TIMEOUT,
        memory_size=512, layers=ops.layers, publish=True, reserved_concurrent_executions=1,
        environment={"variables": {"M11_STAGE": s.stage, "RELEASE_BUCKET": stage.release_bucket}},
        tags=stage.tags, opts=pulumi.ResourceOptions(depends_on=[policy, logs]))
    aws.lambda_.FunctionEventInvokeConfig("drift-check-async", function_name=fn.name, qualifier=fn.version,
                                          maximum_event_age_in_seconds=600, maximum_retry_attempts=0)
    sched = naming.function(s.stage, "drift-check")
    scheduler_policy = stage.role_policy("scheduler-drift", naming.role(s.stage, "scheduler"), fn.qualified_arn.apply(
        lambda arn: [{"Sid": "InvokeDriftCheck", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": arn}]),
        opts=pulumi.ResourceOptions(depends_on=[ops.scheduler_role]))
    schedule = aws.scheduler.Schedule(
        "drift-check-schedule", name=sched, group_name="default", schedule_expression=DRIFT_SCHEDULE,
        description=f"m11 {s.stage} drift check (design 4.4.10)", state="ENABLED", flexible_time_window={"mode": "OFF"},
        target={"arn": fn.qualified_arn, "role_arn": ops.scheduler_role.arn,
                "input": json.dumps({"source": "schedule"}),
                "retry_policy": {"maximum_event_age_in_seconds": 600, "maximum_retry_attempts": 0}},
        opts=pulumi.ResourceOptions(depends_on=[scheduler_policy]))
    aws.cloudwatch.MetricAlarm(
        "alarm-drift", name=naming.function(s.stage, "drift"), namespace=DRIFT_NAMESPACE, metric_name="DriftFindings",
        dimensions={"Stage": s.stage}, statistic="Maximum", period=900, evaluation_periods=1, threshold=1,
        comparison_operator="GreaterThanOrEqualToThreshold", treat_missing_data="notBreaching",
        alarm_description=f"m11 {s.stage}: live state differs from the deployed manifest (design 4.4.10)",
        tags=stage.tags)
    return {"driftFunction": fn.name, "driftArn": fn.qualified_arn, "driftSchedule": schedule.name}


def _shadow_worker(stage: Stage, code: dict, layers: list, queue: aws.sqs.Queue, runtime_arn, control_arn,
                   outcomes_arn) -> tuple[aws.iam.Role, aws.lambda_.Function]:
    s = stage.settings
    name = naming.function(s.stage, "shadow-worker")
    role = stage.role("shadow-worker-role", naming.role(s.stage, "shadow-worker"), "lambda.amazonaws.com",
                      extra_tags={"M11Role": "shadow-worker"})
    logs = stage.log_group("shadow-worker-logs", f"/aws/lambda/{name}")
    statements = pulumi.Output.all(queue.arn, runtime_arn, control_arn, outcomes_arn).apply(lambda v: [
        lambda_logs_statement(stage, name),
        {"Sid": "Queue", "Effect": "Allow",
         "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"],
         "Resource": v[0]},
        {"Sid": "Candidate", "Effect": "Allow",
         "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
         "Resource": [v[1], f"{v[1]}/runtime-endpoint/candidate"]},
        {"Sid": "ControlItem", "Effect": "Allow", "Action": "dynamodb:GetItem", "Resource": v[2]},
        {"Sid": "ShadowOutcomes", "Effect": "Allow", "Action": "dynamodb:PutItem", "Resource": v[3]},
    ])
    policy = stage.role_policy("shadow-worker", naming.role(s.stage, "shadow-worker"), statements,
                               opts=pulumi.ResourceOptions(depends_on=[role]))
    fn = aws.lambda_.Function(
        "shadow-worker", name=name, role=role.arn, runtime="python3.13", architectures=["arm64"],
        handler=SHADOW_HANDLER, s3_bucket=stage.release_bucket, s3_key=code["shadow_worker"]["s3Key"],
        timeout=SHADOW_WORKER_TIMEOUT, memory_size=256, layers=layers, publish=True,
        reserved_concurrent_executions=SHADOW_WORKER_CONCURRENCY,
        environment={"variables": {"M11_STAGE": s.stage, "CONTROL_TABLE": naming.table(s.stage, "control"),
                                   "OUTCOMES_TABLE": naming.table(s.stage, "outcomes"), "RUNTIME_ARN": runtime_arn,
                                   "SESSION_STAGE": stage.key}},
        tags=stage.tags, opts=pulumi.ResourceOptions(depends_on=[policy, logs]))
    aws.lambda_.EventSourceMapping("shadow-worker-queue", event_source_arn=queue.arn, function_name=fn.qualified_arn,
                                   batch_size=1, function_response_types=["ReportBatchItemFailures"])
    return role, fn
