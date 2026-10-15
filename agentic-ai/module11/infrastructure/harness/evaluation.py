# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Evaluation resources of a stage.

- Row 16, the code-based evaluator `custom.no_credential_leakage`: Lambda `m11-<stage>-eval-no-credential-leakage`
  (code from the sealed `releaseInputs.evaluatorCode` zip, published like every Lambda here, P2; the evaluator
  names the published version's qualified ARN, so each release's evaluator runs exactly its sealed code), its
  role and 30-day log group, and the `AgentcoreEvaluator` (session level, code-based Lambda config). Enabled in
  every stage; `enableEvaluator=false` turns it off.
 is settled live (`test_live_evaluate`): `Evaluate` invokes the Lambda with the CALLER's
  credentials (the denial names the eval-runner session and `lambda:InvokeFunction` on the qualified ARN), so
  the eval-runner role gets `lambda:InvokeFunction` on the published version and the design's second path, a
  Lambda permission for the AgentCore service principal, is not created.
  The stack output `evaluatorIds` maps the logical ID to the real evaluator ID; `release deploy` records it in
  the deployed manifest (`deployment.<stage>.evaluatorIds`), which is how the runner resolves it.
- Row 15, online evaluation configs (`onlineEval.enabled`), scored by the gated judge evaluators
  (`ONLINE_EVALUATORS`) into the service's dedicated result group `/aws/bedrock-agentcore/evaluations/results/<id>`:
  * rollout stages (dev, prod; the filter key is `attributes.m11.variant`): the candidate-
    weighted pair `m11_<stagekey>_oe_candidate` (`m11.variant Equals candidate`, 100%) on the candidate endpoint's
    log group and `m11_<stagekey>_oe_stable` (`Equals stable`, `samplingPct`) on stable's. Equality filters
    exclude eval and shadow sessions by construction (N10);
  * staging: one config `m11_staging_oe` on the staging endpoint, `NotEquals eval` and `NotEquals shadow`, so
    gate and warm-up sessions are never scored and the Gateway A/B sessions (`variant=staging`) are.
  The execution role `m11-<stage>-online-eval` reads the endpoint log groups and `aws/spans` and writes only the
  stage's own result groups (`m11_<stagekey>_oe*`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pulumi
import pulumi_aws as aws

from module11.release import eval_names, naming

from . import config_model as CM
from .base import Stage, lambda_logs_statement

EVALUATOR_TIMEOUT_SECONDS = 30


@dataclass
class Evaluators:
    ids: dict = field(default_factory=dict)             # logical ID -> Output of the real evaluator ID
    arns: list = field(default_factory=list)            # Outputs: ARNs the eval runner may `Evaluate` with
    function_arns: list = field(default_factory=list)   # Outputs: published Lambda versions it may invoke


ONLINE_EVALUATORS = ("Builtin.GoalSuccessRate", "Builtin.Correctness")   # thresholds.yaml's gated judges
VARIANT_KEY = "attributes.m11.variant"     # the span attribute's filter key, not a secret  # gitleaks:allow


def online_eval_specs(stage: str, sampling_pct: float) -> list[dict]:
    """The online-evaluation configs for one stage: [{name, endpoint, samplingPct, filters}]."""
    key = naming.stagekey(stage)
    if naming.is_mr(stage):
        return []
    if stage == "staging":
        return [{"name": f"m11_{key}_oe", "endpoint": "staging", "samplingPct": float(sampling_pct),
                 "filters": [{"key": VARIANT_KEY, "operator": "NotEquals", "value": v} for v in ("eval", "shadow")]}]
    return [{"name": f"m11_{key}_oe_candidate", "endpoint": "candidate", "samplingPct": 100.0,
             "filters": [{"key": VARIANT_KEY, "operator": "Equals", "value": "candidate"}]},
            {"name": f"m11_{key}_oe_stable", "endpoint": "stable", "samplingPct": float(sampling_pct),
             "filters": [{"key": VARIANT_KEY, "operator": "Equals", "value": "stable"}]}]


def online_eval(stage: Stage, *, runtime) -> dict:
    """Row 15: the stage's online-evaluation configs and their execution role. Returns {name: config ARN}."""
    s = stage.settings
    specs = online_eval_specs(s.stage, (s.onlineEval or {}).get("samplingPct", 10))
    if not specs:
        return {}
    role_name = naming.role(s.stage, "online-eval")
    role = stage.role("online-eval-role", role_name, "bedrock-agentcore.amazonaws.com",
                      extra_tags={"M11Role": "online-eval"},
                      source_arn=stage.arn("bedrock-agentcore", "online-evaluation-config/*"))
    # Config IDs are `<name>-<suffix>`: m11_staging_oe-..., m11_prod_oe_candidate-...
    results = stage.log_group_arn(f"/aws/bedrock-agentcore/evaluations/results/m11_{stage.key}_oe*")
    statements = runtime.agent_runtime_id.apply(lambda rid: [
        {"Sid": "TraceReads", "Effect": "Allow",
         "Action": ["logs:StartQuery", "logs:GetQueryResults", "logs:FilterLogEvents", "logs:GetLogEvents"],
         "Resource": [arn for g in [f"/aws/bedrock-agentcore/runtimes/{rid}-{e}" for e in s.endpoints] + ["aws/spans"]
                      for arn in (stage.log_group_arn(g), stage.log_group_arn(g) + ":*")]},
        {"Sid": "Describe", "Effect": "Allow",
         "Action": ["logs:DescribeLogGroups", "logs:DescribeIndexPolicies", "logs:PutIndexPolicy"], "Resource": "*"},
        {"Sid": "Results", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": results},
    ])
    policy = stage.role_policy("online-eval", role_name, statements, opts=pulumi.ResourceOptions(depends_on=[role]))
    out = {}
    for spec in specs:
        endpoint = spec["endpoint"]
        config = aws.bedrock.AgentcoreOnlineEvaluationConfig(
            spec["name"].replace("_", "-"), online_evaluation_config_name=spec["name"],
            description=f"m11 {s.stage}: {endpoint} sessions scored by {', '.join(ONLINE_EVALUATORS)}",
            rule={"sampling_config": {"sampling_percentage": spec["samplingPct"]},
                  "filters": [{"key": f["key"], "operator": f["operator"], "value": {"string_value": f["value"]}}
                              for f in spec["filters"]]},
            data_source_config={"cloudwatch_logs": {
                "log_group_names": [runtime.agent_runtime_id.apply(
                    lambda rid, e=endpoint: f"/aws/bedrock-agentcore/runtimes/{rid}-{e}")],
                "service_names": [f"{naming.runtime_name(s.stage)}.{endpoint}"]}},
            evaluators=[{"evaluator_id": e} for e in ONLINE_EVALUATORS], evaluation_execution_role_arn=role.arn,
            enable_on_create=True, tags=stage.tags, opts=pulumi.ResourceOptions(depends_on=[policy]))
        out[spec["name"]] = config.online_evaluation_config_arn
    return out


def declare(stage: Stage, *, inputs: CM.ReleaseInputs, enable_evaluator: bool, online_eval: bool = False) -> Evaluators:
    # Online evaluation configs are declared by `online_eval()` after the runtime exists (program.py).
    if not enable_evaluator:
        return Evaluators()
    name = eval_names.evaluator_function(stage.name)
    role = stage.role("eval-leak-role", name, "lambda.amazonaws.com", extra_tags={"M11Role": "evaluator"})
    logs = stage.log_group("eval-leak-logs", f"/aws/lambda/{name}")
    policy = stage.role_policy("eval-leak", name, [lambda_logs_statement(stage, name)],
                               opts=pulumi.ResourceOptions(depends_on=[role]))
    function = aws.lambda_.Function(
        "eval-leak", name=name, role=role.arn, runtime="python3.13", architectures=["arm64"],
        handler="handler.handler", s3_bucket=stage.release_bucket, s3_key=inputs.evaluatorCode["s3Key"],
        timeout=EVALUATOR_TIMEOUT_SECONDS, memory_size=256, publish=True, tags=stage.tags,
        opts=pulumi.ResourceOptions(depends_on=[policy, logs]))
    evaluator = aws.bedrock.AgentcoreEvaluator(
        "eval-leak-evaluator", evaluator_name=eval_names.evaluator_name(stage.name), level="SESSION",
        description="Scores 1.0 when no credential pattern appears anywhere in the session's spans (hard gate)",
        evaluator_config={"code_based": {"lambda_config": {
            "lambda_arn": function.qualified_arn, "lambda_timeout_in_seconds": EVALUATOR_TIMEOUT_SECONDS}}},
        tags=stage.tags)
    return Evaluators({eval_names.EVALUATOR_LOGICAL: evaluator.evaluator_id}, [evaluator.evaluator_arn],
                      [function.qualified_arn])
