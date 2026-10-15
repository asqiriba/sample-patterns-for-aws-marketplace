# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure IAM renderers for m11-platform. No Pulumi, no AWS calls.

- `operator_trust(principals)`: the two-statement operator trust template.
- `deployer_policies(ctx, stage)`: the stage deployer's managed policies, split by area so each
  stays under IAM's 6,144-character managed-policy limit. A stage deployer is the only
  principal that changes a stage. Resources with generated IDs (REST APIs, usage plans, API
  keys, guardrails) are reached only through `Stage` tags plus explicit Denies.
- `preview_deployer_policies(ctx)`: the same for `mr-*` stacks: preview state backend only,
  `m11-mr*` names, and roles created only with `m11-mr-boundary` attached (H3).
- `mr_boundary(ctx)`: the mr-* permissions boundary document.
- `ci_start_policy(project_arn, names)`: the MR jobs' StartBuild policy (used with the pipeline).

Every policy here is checked by the CrossGuard pack (P1a-P1d) when m11-platform is previewed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from module11.release import naming
from module11.infrastructure.policy.rules import CI_DENY_KEYS

PROJECT = naming.PROJECT_TAG
MANAGED_POLICY_LIMIT = 6144
INLINE_LIMIT = 10240
OPERATOR_SESSION = "m11-operator-*"
DEPLOYER_SESSION_SECONDS = 4 * 3600
STAGES = naming.STAGES
COGNITO_STAGES = ("prod",)          # dev and staging have no browser client


@dataclass(frozen=True)
class Ctx:
    """Account facts and shared names every renderer needs (no literal IDs in code)."""
    account: str
    region: str
    state_key_arn: str
    preview_key_arn: str
    user_pool_id: str
    state_table: str
    partition: str = "aws"

    @property
    def release_bucket(self) -> str:
        return naming.RELEASE_BUCKET.format(account=self.account, region=self.region)

    @property
    def pipeline_bucket(self) -> str:
        return naming.PIPELINE_BUCKET.format(account=self.account, region=self.region)

    @property
    def state_bucket(self) -> str:
        return naming.STATE_BUCKET.format(account=self.account, region=self.region)

    @property
    def preview_state_bucket(self) -> str:
        return naming.PREVIEW_STATE_BUCKET.format(account=self.account, region=self.region)

    def arn(self, service: str, resource: str, *, region: bool = True, account: bool = True) -> str:
        return (f"arn:{self.partition}:{service}:{self.region if region else ''}:"
                f"{self.account if account else ''}:{resource}")

    def iam(self, resource: str) -> str:
        return f"arn:{self.partition}:iam::{self.account}:{resource}"

    def s3(self, bucket: str, key: str | None = None) -> str:
        return f"arn:{self.partition}:s3:::{bucket}" + (f"/{key}" if key is not None else "")

    @property
    def boundary_arn(self) -> str:
        return self.iam(f"policy/{naming.BOUNDARY}")

    @property
    def user_pool_arn(self) -> str:
        return self.arn("cognito-idp", f"userpool/{self.user_pool_id}")

    def apigw(self, path: str) -> str:
        return f"arn:{self.partition}:apigateway:{self.region}::/{path}"


def doc(statements: Iterable[dict]) -> dict:
    return {"Version": "2012-10-17", "Statement": [s for s in statements if s]}


def chars(policy: dict) -> int:
    """IAM counts a policy's characters without whitespace; compact JSON is the same measure."""
    return len(json.dumps(policy, separators=(",", ":")))


def allow(sid: str, actions: Sequence[str] | str, resources: Sequence[str] | str, condition: dict | None = None) -> dict:
    out = {"Sid": sid, "Effect": "Allow", "Action": _one(actions), "Resource": _one(resources)}
    if condition:
        out["Condition"] = condition
    return out


def deny(sid: str, actions: Sequence[str] | str, resources: Sequence[str] | str, condition: dict | None = None) -> dict:
    out = allow(sid, actions, resources, condition)
    out["Effect"] = "Deny"
    return out


def _one(value):
    if isinstance(value, str):
        return value
    value = list(dict.fromkeys(value))
    return value[0] if len(value) == 1 else value


# ---------------------------------------------------------------- trust policies
def operator_trust(principals: Sequence[str]) -> list[dict]:
    """Two statements: sts:SetSourceIdentity is authorized with its own condition keys."""
    if not principals:
        return []
    principal = {"AWS": list(principals)}
    return [
        {"Sid": "OperatorAssume", "Effect": "Allow", "Principal": principal, "Action": "sts:AssumeRole",
         "Condition": {"StringLike": {"sts:RoleSessionName": OPERATOR_SESSION},
                       "Null": {"sts:SourceIdentity": "false"}}},
        {"Sid": "OperatorSourceIdentity", "Effect": "Allow", "Principal": principal,
         "Action": "sts:SetSourceIdentity", "Condition": {"Null": {"sts:SourceIdentity": "false"}}},
    ]


def role_trust(principals: Sequence[str], role_arns: Sequence[str] = ()) -> dict:
    """Operator template plus trusted roles (the pipeline's CodeBuild project roles, the preview deployer)."""
    statements = operator_trust(principals)
    if role_arns:
        statements.append({"Sid": "TrustedRoles", "Effect": "Allow", "Principal": {"AWS": list(role_arns)},
                           "Action": ["sts:AssumeRole", "sts:SetSourceIdentity"]})
    return doc(statements)


def service_trust(service: str, account: str, source_arn: str | None = None) -> dict:
    condition: dict[str, Any] = {"StringEquals": {"aws:SourceAccount": account}}
    if source_arn:
        condition["ArnLike"] = {"aws:SourceArn": source_arn}
    return doc([{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole",
                 "Condition": condition}])


# ---------------------------------------------------------------- scopes
@dataclass(frozen=True)
class Scope:
    """What one deployer may touch: its stage's names and tags, or every `mr-*` stage."""
    stage: str                 # dev | staging | prod | mr (preview)
    preview: bool

    @property
    def dash(self) -> str:     # m11-dev-, m11-mr
        return "m11-mr" if self.preview else f"m11-{self.stage}-"

    @property
    def under(self) -> str:    # m11_dev_, m11_mr
        return "m11_mr" if self.preview else f"m11_{self.stage}_"

    @property
    def tag_value(self) -> str:
        return "mr-*" if self.preview else self.stage

    @property
    def eq(self) -> str:
        return "StringLike" if self.preview else "StringEquals"

    @property
    def neq(self) -> str:
        return "StringNotLike" if self.preview else "StringNotEquals"


def _request_tags(scope: Scope) -> dict:
    if scope.preview:
        return {"StringLike": {"aws:RequestTag/Stage": "mr-*"},
                "StringEquals": {"aws:RequestTag/Project": PROJECT},
                "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Stage"]}}
    return {"StringEquals": {"aws:RequestTag/Stage": scope.stage, "aws:RequestTag/Project": PROJECT},
            "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Stage"]}}


def generated_id_statements(ctx: Ctx, scope: Scope, *, creates: bool = True) -> list[dict]:
    """The tag-scoped templates for REST APIs, usage plans, API keys and guardrails."""
    rest = [ctx.apigw(f"{k}/*") for k in ("restapis", "usageplans", "apikeys")]
    tags = ctx.apigw("tags/*")
    guard = ctx.arn("bedrock", "guardrail/*")
    resource_tag = {scope.eq: {"aws:ResourceTag/Stage": scope.tag_value}}
    out = []
    if creates:
        out += [
            allow("CreateApi", "apigateway:POST", [ctx.apigw(k) for k in ("restapis", "usageplans", "apikeys")],
                  _request_tags(scope)),
            allow("CreateGuardrail", "bedrock:CreateGuardrail", "*", _request_tags(scope)),
            allow("CreateTagGuardrail", "bedrock:TagResource", guard, _request_tags(scope)),
        ]
    out += [
        allow("ManageApi", ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH",
                            "apigateway:DELETE"], rest, resource_tag),
        allow("ManageGuardrail", ["bedrock:GetGuardrail", "bedrock:UpdateGuardrail", "bedrock:CreateGuardrailVersion",
                                  "bedrock:DeleteGuardrail", "bedrock:ListTagsForResource"], guard, resource_tag),
        allow("ApiTags", ["apigateway:GET", "apigateway:PUT", "apigateway:DELETE"], tags),
        # After DeleteRestApi the provider polls GetRestApi until NotFound; a deleted API has no tags.
        allow("ReadDeletedApi", "apigateway:GET", ctx.apigw("restapis/*"), {"Null": {"aws:ResourceTag/Stage": "true"}}),
        allow("TagOwnGuardrail", ["bedrock:TagResource", "bedrock:UntagResource"], guard, resource_tag),
        deny("DenyForeignStageTagging", ["apigateway:PUT", "apigateway:DELETE", "bedrock:TagResource",
                                         "bedrock:UntagResource"], [tags, guard],
             {"ForAnyValue:StringEquals": {"aws:TagKeys": ["Stage", "Project"]},
              scope.neq: {"aws:RequestTag/Stage": scope.tag_value}}),
        deny("DenyRetagOtherStageGuardrail", "bedrock:TagResource", guard,
             {"Null": {"aws:ResourceTag/Stage": "false"}, scope.neq: {"aws:ResourceTag/Stage": scope.tag_value}}),
        deny("DenyRetagForeignProjectGuardrail", "bedrock:TagResource", guard,
             {"Null": {"aws:ResourceTag/Project": "false"}, "StringNotEquals": {"aws:ResourceTag/Project": PROJECT}}),
        deny("DenyForeignApi", ["apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"], rest,
             {scope.neq: {"aws:ResourceTag/Stage": scope.tag_value}}),
        deny("DenyForeignGuardrail", ["bedrock:UpdateGuardrail", "bedrock:CreateGuardrailVersion",
                                      "bedrock:DeleteGuardrail"], guard,
             {scope.neq: {"aws:ResourceTag/Stage": scope.tag_value}}),
    ]
    return out


IAM_ROLE_WRITE = ["iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
                  "iam:TagRole", "iam:UntagRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy"]
IAM_ROLE_READ = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
                 "iam:ListRoleTags", "iam:ListInstanceProfilesForRole"]
LAMBDA_FUNCTION = ["lambda:CreateFunction", "lambda:DeleteFunction", "lambda:GetFunction",
                   "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
                   "lambda:PublishVersion", "lambda:ListVersionsByFunction", "lambda:GetFunctionCodeSigningConfig",
                   "lambda:GetPolicy", "lambda:AddPermission", "lambda:RemovePermission", "lambda:TagResource",
                   "lambda:UntagResource", "lambda:ListTags", "lambda:GetFunctionConcurrency",
                   "lambda:PutFunctionConcurrency", "lambda:DeleteFunctionConcurrency",
                   "lambda:GetFunctionRecursionConfig", "lambda:GetRuntimeManagementConfig",
                   "lambda:GetFunctionEventInvokeConfig", "lambda:PutFunctionEventInvokeConfig",
                   "lambda:UpdateFunctionEventInvokeConfig", "lambda:DeleteFunctionEventInvokeConfig",
                   "lambda:InvokeFunction"]
LOG_GROUP = ["logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
             "logs:TagResource", "logs:UntagResource", "logs:ListTagsForResource", "logs:TagLogGroup",
             "logs:UntagLogGroup", "logs:ListTagsLogGroup"]
TABLE = ["dynamodb:CreateTable", "dynamodb:DeleteTable", "dynamodb:DescribeTable", "dynamodb:UpdateTable",
         "dynamodb:DescribeTimeToLive", "dynamodb:UpdateTimeToLive", "dynamodb:DescribeContinuousBackups",
         "dynamodb:UpdateContinuousBackups", "dynamodb:TagResource", "dynamodb:UntagResource",
         "dynamodb:ListTagsOfResource", "dynamodb:DescribeKinesisStreamingDestination", "dynamodb:GetResourcePolicy"]
BUCKET = ["s3:CreateBucket", "s3:DeleteBucket", "s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketAcl",
          "s3:GetBucketCORS", "s3:GetBucketLocation", "s3:GetBucketLogging", "s3:GetBucketNotification",
          "s3:GetBucketObjectLockConfiguration", "s3:GetBucketOwnershipControls", "s3:GetBucketPolicy",
          "s3:GetBucketPolicyStatus", "s3:GetBucketPublicAccessBlock", "s3:GetBucketRequestPayment",
          "s3:GetBucketTagging", "s3:GetBucketVersioning", "s3:GetBucketWebsite", "s3:GetAccelerateConfiguration",
          "s3:GetEncryptionConfiguration", "s3:GetLifecycleConfiguration", "s3:GetReplicationConfiguration",
          "s3:PutBucketTagging", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration",
          "s3:PutLifecycleConfiguration", "s3:PutBucketPublicAccessBlock", "s3:PutBucketOwnershipControls",
          "s3:PutBucketPolicy", "s3:DeleteBucketPolicy"]
OBJECT = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
PARAM = ["ssm:PutParameter", "ssm:GetParameter", "ssm:GetParameters", "ssm:DeleteParameter",
         "ssm:AddTagsToResource", "ssm:RemoveTagsFromResource", "ssm:ListTagsForResource"]
AGENTCORE_CREATE = ["bedrock-agentcore:CreateAgentRuntime", "bedrock-agentcore:CreateMemory",
                    "bedrock-agentcore:CreateConfigurationBundle", "bedrock-agentcore:CreateEvaluator",
                    "bedrock-agentcore:ListAgentRuntimes",
                    "bedrock-agentcore:ListAgentRuntimeVersions", "bedrock-agentcore:ListAgentRuntimeEndpoints",
                    "bedrock-agentcore:ListMemories", "bedrock-agentcore:ListConfigurationBundles",
                    "bedrock-agentcore:ListEvaluators", "cloudformation:CreateResource",
                    "cloudformation:GetResource", "cloudformation:UpdateResource", "cloudformation:DeleteResource",
                    "cloudformation:GetResourceRequestStatus", "cloudformation:ListResources"]
AGENTCORE_RUNTIME = ["bedrock-agentcore:UpdateAgentRuntime", "bedrock-agentcore:DeleteAgentRuntime",
                     "bedrock-agentcore:GetAgentRuntime", "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                     "bedrock-agentcore:UpdateAgentRuntimeEndpoint", "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                     "bedrock-agentcore:GetAgentRuntimeEndpoint", "bedrock-agentcore:PutResourcePolicy",
                     "bedrock-agentcore:GetResourcePolicy", "bedrock-agentcore:DeleteResourcePolicy"]
AGENTCORE_TAGS = ["bedrock-agentcore:TagResource", "bedrock-agentcore:UntagResource",
                  "bedrock-agentcore:ListTagsForResource"]
AGENTCORE_MEMORY = ["bedrock-agentcore:GetMemory", "bedrock-agentcore:UpdateMemory", "bedrock-agentcore:DeleteMemory"]
AGENTCORE_BUNDLE = ["bedrock-agentcore:GetConfigurationBundle", "bedrock-agentcore:UpdateConfigurationBundle",
                    "bedrock-agentcore:DeleteConfigurationBundle", "bedrock-agentcore:GetConfigurationBundleVersion",
                    "bedrock-agentcore:ListConfigurationBundleVersions"]
WORKLOAD_IDENTITY = ["bedrock-agentcore:CreateWorkloadIdentity", "bedrock-agentcore:GetWorkloadIdentity",
                     "bedrock-agentcore:UpdateWorkloadIdentity", "bedrock-agentcore:DeleteWorkloadIdentity"]
COGNITO_CLIENT = ["cognito-idp:CreateUserPoolClient", "cognito-idp:DescribeUserPoolClient",
                  "cognito-idp:UpdateUserPoolClient", "cognito-idp:DeleteUserPoolClient",
                  "cognito-idp:CreateManagedLoginBranding", "cognito-idp:DescribeManagedLoginBranding",
                  "cognito-idp:DescribeManagedLoginBrandingByClient", "cognito-idp:UpdateManagedLoginBranding",
                  "cognito-idp:DeleteManagedLoginBranding", "cognito-idp:DescribeUserPool",
                  "cognito-idp:ListUserPoolClients"]      # read by `--reapply`'s refresh of the branding
NO_RESOURCE = ["sts:GetCallerIdentity", "logs:DescribeLogGroups", "ssm:DescribeParameters",
               "lambda:ListLayerVersions"]
# Stage deployers only (no resource type, P1b): online-evaluation configs, the staging Gateway, and the
# trace index policy the service sets on aws/spans for online evaluation.
STAGE_NO_RESOURCE = ["bedrock-agentcore:CreateOnlineEvaluationConfig", "bedrock-agentcore:ListOnlineEvaluationConfigs",
                     "logs:DescribeIndexPolicies", "logs:PutIndexPolicy"]
GATEWAY_NO_RESOURCE = ["bedrock-agentcore:CreateGateway", "bedrock-agentcore:ListGateways"]
AGENTCORE_ONLINE_EVAL = ["bedrock-agentcore:GetOnlineEvaluationConfig", "bedrock-agentcore:UpdateOnlineEvaluationConfig",
                         "bedrock-agentcore:DeleteOnlineEvaluationConfig"]
AGENTCORE_GATEWAY = ["bedrock-agentcore:GetGateway", "bedrock-agentcore:UpdateGateway", "bedrock-agentcore:DeleteGateway",
                     "bedrock-agentcore:CreateGatewayTarget", "bedrock-agentcore:GetGatewayTarget",
                     "bedrock-agentcore:UpdateGatewayTarget", "bedrock-agentcore:DeleteGatewayTarget",
                     "bedrock-agentcore:ListGatewayTargets"]
GATEWAY_STAGES = ("staging",)


def _state_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    """Pulumi DIY backend objects for this deployer's own stacks, plus the stack references it reads."""
    if scope.preview:
        bucket, key = ctx.preview_state_bucket, ctx.preview_key_arn
        own = [ctx.s3(bucket, ".pulumi/stacks/m11-harness/*"), ctx.s3(bucket, ".pulumi/history/m11-harness/*"),
               ctx.s3(bucket, ".pulumi/backups/m11-harness/*"), ctx.s3(bucket, ".pulumi/locks/*")]
        refs = [ctx.s3(bucket, ".pulumi/meta.yaml")]
    else:
        bucket, key = ctx.state_bucket, ctx.state_key_arn
        stage = scope.stage
        own = [ctx.s3(bucket, f".pulumi/stacks/m11-harness/{stage}.json"),
               ctx.s3(bucket, f".pulumi/stacks/m11-harness/{stage}.json.bak"),
               ctx.s3(bucket, f".pulumi/history/m11-harness/{stage}/*"),
               ctx.s3(bucket, f".pulumi/backups/m11-harness/{stage}/*"),
               ctx.s3(bucket, f".pulumi/locks/m11-harness/{stage}/*"),
               ctx.s3(bucket, f".pulumi/locks/organization/m11-harness/{stage}/*")]
        refs = [ctx.s3(bucket, ".pulumi/meta.yaml"),
                ctx.s3(bucket, ".pulumi/stacks/module10-exposure/workshop.json"),
                ctx.s3(bucket, ".pulumi/stacks/m11-platform/shared.json")]
    return [
        allow("StateList", ["s3:ListBucket", "s3:GetBucketLocation"], ctx.s3(bucket)),
        allow("StateObjects", ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], own),
        allow("StateRefs", "s3:GetObject", refs),
        allow("StateKey", ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"], key),
    ]


def _release_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    rb = ctx.release_bucket
    stage_part = "mr-*" if scope.preview else scope.stage
    reads = [ctx.s3(rb, p) for p in ("releases/*", "handlers/*", "layers/*", "evidence/*", "snapshots/*",
                                     "builds/*")]
    writes = ([ctx.s3(rb, "release-inputs/mr-*"), ctx.s3(rb, "ledger/mr-*")] if scope.preview else
              [ctx.s3(rb, f"release-inputs/{stage_part}/*"), ctx.s3(rb, f"ledger/{stage_part}/*")])
    return [
        allow("ReleaseList", "s3:ListBucket", ctx.s3(rb)),
        allow("ReleaseRead", "s3:GetObject", reads),
        allow("ReleaseWrite", ["s3:GetObject", "s3:PutObject"], writes),
        allow("NoResourceType", NO_RESOURCE + AGENTCORE_CREATE, "*"),
    ]


def _compute_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    d, u = scope.dash, scope.under
    roles = ctx.iam(f"role/{d}*")
    stage_params = ([ctx.arn("ssm", f"parameter/m11/devops-companion/mr-*")] if scope.preview else
                    [ctx.arn("ssm", f"parameter/m11/devops-companion/{scope.stage}/{leaf}")
                     for leaf in ("current", "candidate", "applied", "drift")])
    buckets = ([ctx.s3(f"m11-mr*")] if scope.preview else
               [ctx.s3(naming.artifacts_bucket(scope.stage, ctx.account, ctx.region))])
    objects = [b + "/*" for b in buckets]
    groups = [ctx.arn("logs", f"log-group:/aws/lambda/{d}*"),
              ctx.arn("logs", f"log-group:/aws/bedrock-agentcore/runtimes/{u}*"),
              ctx.arn("logs", f"log-group:/aws/apigateway/{d}*")]
    if not scope.preview:   # the online-evaluation configs' result groups
        groups.append(ctx.arn("logs", f"log-group:/aws/bedrock-agentcore/evaluations/results/{u}*"))
    out = [
        allow("RolesRead", IAM_ROLE_READ, roles),
        allow("PassRoles", "iam:PassRole", roles,
              {"StringEquals": {"iam:PassedToService": ["lambda.amazonaws.com", "bedrock-agentcore.amazonaws.com"]
                                + ([] if scope.preview else ["scheduler.amazonaws.com"])}}),
        allow("Functions", LAMBDA_FUNCTION, ctx.arn("lambda", f"function:{d}*")),
        allow("Layers", ["lambda:PublishLayerVersion", "lambda:GetLayerVersion", "lambda:DeleteLayerVersion"],
              ctx.arn("lambda", f"layer:{d}*")),
        allow("LogGroups", LOG_GROUP, groups),
        allow("Tables", TABLE + ["dynamodb:GetItem", "dynamodb:PutItem"], ctx.arn("dynamodb", f"table/{d}*")),
        allow("Buckets", BUCKET, buckets),
        allow("BucketObjects", OBJECT, objects),
        allow("Parameters", PARAM, stage_params),
        # release deploy step 1: latch, freeze and prod /current (the plan's base) are read, never written.
        allow("ReleaseStateRead", "ssm:GetParameter", ctx.arn("ssm", "parameter/m11/devops-companion/latch/*")),
        allow("ReleaseState", ["ssm:GetParameter"], [ctx.arn("ssm", "parameter/m11/devops-companion/freeze"),
                                                      ctx.arn("ssm", "parameter/m11/devops-companion/prod/current")]),
        deny("DenyDeployerRoles", IAM_ROLE_WRITE + ["iam:PassRole"], ctx.iam("role/m11-*-deployer")),
    ]
    if scope.preview:
        boundary = {"StringEquals": {"iam:PermissionsBoundary": ctx.boundary_arn}}
        out += [
            allow("RolesWithBoundary", ["iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy"], roles, boundary),
            allow("RolesWrite", ["iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy", "iam:TagRole",
                                 "iam:UntagRole"], roles),
            deny("DenyBoundaryRemoval", ["iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary"],
                 roles),
            allow("SharedOutputs", "ssm:GetParameter", ctx.arn("ssm", f"parameter{naming.SHARED_OUTPUTS}")),
        ]
    else:
        out += [
            allow("RolesWrite", IAM_ROLE_WRITE, roles),
            deny("DenyPlatformRoleTrust", ["iam:UpdateAssumeRolePolicy", "iam:DeleteRole", "iam:TagRole",
                                           "iam:UntagRole"], ctx.iam(f"role/{d}eval-runner")),
        ]
    return out


def _agentcore_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    u = scope.under
    runtime = ctx.arn("bedrock-agentcore", f"runtime/{u}*")
    memory = ctx.arn("bedrock-agentcore", f"memory/{u}*")
    bundle = ctx.arn("bedrock-agentcore", f"configuration-bundle/{u}*")
    evaluator = ctx.arn("bedrock-agentcore", f"evaluator/{u}*")
    directory = ctx.arn("bedrock-agentcore", "workload-identity-directory/default")
    generated = [ctx.arn("bedrock-agentcore", p) for p in
                 ("runtime/*", "memory/*", "evaluator/*", "configuration-bundle/*",
                  "workload-identity-directory/default/workload-identity/*")]
    if not scope.preview and scope.stage in GATEWAY_STAGES:
        # CreateGateway tags the gateway against the literal `gateway/*` before its ID exists (probed as the
        # staging deployer: AccessDenied on TagResource for gateway/*), like CreateAgentRuntime's endpoint.
        generated.append(ctx.arn("bedrock-agentcore", "gateway/*"))
    request = {scope.eq: {"aws:RequestTag/Stage": scope.tag_value},
               "StringEquals": {"aws:RequestTag/Project": PROJECT}} if scope.preview else \
        {"StringEquals": {"aws:RequestTag/Stage": scope.stage, "aws:RequestTag/Project": PROJECT}}
    return [
        # CreateAgentRuntime also authorizes CreateAgentRuntimeEndpoint (its DEFAULT endpoint) against the
        # literal `runtime/*`, before the runtime ID exists; the create carries the stage's tags.
        # Creates with tags are tagging requests on a resource whose ID does not exist yet (like the
        # guardrail's create-time TagResource), and CreateAgentRuntime also creates its DEFAULT
        # endpoint and workload identity: request tags only, plus the Denies below for tagged resources.
        allow("CreateTime", ["bedrock-agentcore:CreateAgentRuntimeEndpoint", "bedrock-agentcore:TagResource",
                             "bedrock-agentcore:CreateWorkloadIdentity"], generated, request),
        # Observed live: ListConfigurationBundleVersions is authorized against `configuration-bundle/*`.
        allow("ListBundleVersions", "bedrock-agentcore:ListConfigurationBundleVersions",
              ctx.arn("bedrock-agentcore", "configuration-bundle/*")),
        deny("DenyForeignProject", ["bedrock-agentcore:CreateAgentRuntimeEndpoint",
                                    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                                    "bedrock-agentcore:DeleteAgentRuntimeEndpoint", "bedrock-agentcore:TagResource",
                                    "bedrock-agentcore:UntagResource"], generated,
             {"Null": {"aws:ResourceTag/Project": "false"}, "StringNotEquals": {"aws:ResourceTag/Project": PROJECT}}),
        deny("DenyRetagOtherStage", ["bedrock-agentcore:TagResource", "bedrock-agentcore:UntagResource"], generated,
             {"Null": {"aws:ResourceTag/Stage": "false"}, scope.neq: {"aws:ResourceTag/Stage": scope.tag_value}}),
        allow("Runtime", AGENTCORE_RUNTIME + AGENTCORE_TAGS, runtime),
        allow("Memory", AGENTCORE_MEMORY + AGENTCORE_TAGS + ["bedrock-agentcore:ListMemoryRecords",
                                                             "bedrock-agentcore:BatchDeleteMemoryRecords"], memory),
        allow("Bundle", AGENTCORE_BUNDLE + AGENTCORE_TAGS, bundle),
        allow("Evaluators", ["bedrock-agentcore:GetEvaluator", "bedrock-agentcore:UpdateEvaluator",
                             "bedrock-agentcore:DeleteEvaluator"] + AGENTCORE_TAGS, evaluator),
        allow("WorkloadIdentity", WORKLOAD_IDENTITY + AGENTCORE_TAGS, [directory, f"{directory}/workload-identity/{u}*"]),
    ]


def _phase8_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    """Stage deployers only: online-evaluation configs (IDs `<name>-<suffix>`, names m11_<stage>_oe*), the
    trace index policy the service sets on aws/spans, and in staging the A/B Gateway and its workload identity."""
    if scope.preview:
        return []
    directory = ctx.arn("bedrock-agentcore", "workload-identity-directory/default")
    gateway = scope.stage in GATEWAY_STAGES
    out = [
        allow("Phase8NoResourceType", STAGE_NO_RESOURCE + (GATEWAY_NO_RESOURCE if gateway else []), "*"),
        allow("OnlineEval", AGENTCORE_ONLINE_EVAL + AGENTCORE_TAGS,
              ctx.arn("bedrock-agentcore", f"online-evaluation-config/{scope.under}*")),
    ]
    if gateway:
        out += [
            allow("Gateway", AGENTCORE_GATEWAY + AGENTCORE_TAGS, ctx.arn("bedrock-agentcore", f"gateway/{scope.dash}*")),
            # CreateGateway creates its workload identity `<gateway id>` in the default directory, untagged (probed).
            allow("GatewayIdentity", WORKLOAD_IDENTITY + AGENTCORE_TAGS,
                  [directory, f"{directory}/workload-identity/{scope.dash}*"]),
        ]
    return out


def _edge_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    out = generated_id_statements(ctx, scope)
    # R9: access logs only when the account's API Gateway CloudWatch role exists (read-only check).
    out.append(allow("ApiAccount", "apigateway:GET", ctx.apigw("account")))
    if scope.preview or scope.stage in COGNITO_STAGES:
        out.append(allow("BrowserClients", COGNITO_CLIENT, ctx.user_pool_arn))
    return out


SCHEDULE = ["scheduler:CreateSchedule", "scheduler:GetSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"]
ALARM = ["cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:DescribeAlarms", "cloudwatch:TagResource",
         "cloudwatch:UntagResource", "cloudwatch:ListTagsForResource"]
QUEUE = ["sqs:CreateQueue", "sqs:DeleteQueue", "sqs:GetQueueAttributes", "sqs:SetQueueAttributes", "sqs:GetQueueUrl",
         "sqs:TagQueue", "sqs:UntagQueue", "sqs:ListQueueTags"]
EVENT_SOURCE_MAPPING = ["lambda:GetEventSourceMapping", "lambda:UpdateEventSourceMapping",
                        "lambda:DeleteEventSourceMapping"]
OPS_PARTS = ("state", "compute", "agentcore", "edge", "ops")
PREVIEW_PARTS = ("state", "compute", "agentcore", "edge")


def _ops_statements(ctx: Ctx, scope: Scope) -> list[dict]:
    """Operations: the controller's schedule, its alarms, the shadow queue and the
    worker's event source mapping. Stage deployers only; mr-* stacks have no operations."""
    d = scope.dash
    own_functions = {"ArnLike": {"lambda:FunctionArn": ctx.arn("lambda", f"function:{d}*")}}
    return [
        allow("Schedules", SCHEDULE, ctx.arn("scheduler", f"schedule/default/{d}*")),
        allow("Alarms", ALARM, ctx.arn("cloudwatch", f"alarm:{d}*")),
        allow("Queues", QUEUE, ctx.arn("sqs", f"{d}*")),
        # CreateEventSourceMapping has no resource type; the mapping's own ARN is a generated ID (position 6),
        # so both are scoped to this stage's functions by lambda:FunctionArn.
        allow("CreateEventSourceMapping", ["lambda:CreateEventSourceMapping", "lambda:ListEventSourceMappings"], "*",
              own_functions),
        allow("EventSourceMappings", EVENT_SOURCE_MAPPING, ctx.arn("lambda", "event-source-mapping:*"), own_functions),
        # The provider reads a mapping's tags after every create and refresh; ListTags carries no FunctionArn key.
        allow("EventSourceMappingTags", "lambda:ListTags", ctx.arn("lambda", "event-source-mapping:*")),
    ]


def _split(name: str, parts: dict[str, list[dict]]) -> dict[str, dict]:
    out = {f"{name}-{part}": doc(statements) for part, statements in parts.items()}
    for policy_name, policy in out.items():
        if chars(policy) > MANAGED_POLICY_LIMIT:
            raise ValueError(f"{policy_name} renders to {chars(policy)} characters (limit {MANAGED_POLICY_LIMIT})")
    return out


def deployer_policies(ctx: Ctx, stage: str) -> dict[str, dict]:
    """{managed policy name: document} for m11-<stage>-deployer."""
    if stage not in STAGES:
        raise ValueError(f"stage deployers exist for {STAGES}; mr-* stacks use the preview deployer")
    scope = Scope(stage, False)
    return _split(f"m11-{stage}-deployer", {
        "state": _state_statements(ctx, scope) + _release_statements(ctx, scope),
        "compute": _compute_statements(ctx, scope),
        "agentcore": _agentcore_statements(ctx, scope),
        "edge": _edge_statements(ctx, scope),
        "ops": _ops_statements(ctx, scope) + _phase8_statements(ctx, scope),
    })


def preview_deployer_policies(ctx: Ctx) -> dict[str, dict]:
    """{managed policy name: document} for m11-preview-deployer (the operator path for mr-*)."""
    scope = Scope("mr", True)
    return _split("m11-preview-deployer", {
        "state": _state_statements(ctx, scope) + _release_statements(ctx, scope),
        "compute": _compute_statements(ctx, scope),
        "agentcore": _agentcore_statements(ctx, scope),
        "edge": _edge_statements(ctx, scope),
    })


def preview_infra_policies(ctx: Ctx) -> dict[str, dict]:
    """{managed policy name: document} for m11-preview-infra: the preview deployer's scope
    without the create statements for generated-ID resources (REST APIs, usage plans, API keys, guardrails)
    and without any Cognito write, so MR tooling can only update an existing mr-* stack."""
    scope = Scope("mr", True)
    edge = generated_id_statements(ctx, scope, creates=False)
    edge += [allow("ApiAccount", "apigateway:GET", ctx.apigw("account")),
             allow("BrowserClientsRead", ["cognito-idp:DescribeUserPoolClient", "cognito-idp:DescribeUserPool"],
                   ctx.user_pool_arn)]
    return _split("m11-preview-infra", {
        "state": _state_statements(ctx, scope) + _release_statements(ctx, scope),
        "compute": _compute_statements(ctx, scope),
        "agentcore": _agentcore_statements(ctx, scope),
        "edge": edge,
    })


# ---------------------------------------------------------------- the image build
def image_build_policy(ctx: Ctx, repository_arn: str, log_group: str) -> dict:
    return doc([
        allow("Context", ["s3:GetObject"], ctx.s3(ctx.release_bucket, "builds/*")),
        allow("Registry", "ecr:GetAuthorizationToken", "*"),
        allow("Push", ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                       "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                       "ecr:DescribeImages"], repository_arn),
        allow("Logs", ["logs:CreateLogStream", "logs:PutLogEvents"],
              ctx.arn("logs", f"log-group:{log_group}:*")),
    ])


# ---------------------------------------------------------------- the mr-* boundary
def mr_boundary(ctx: Ctx, image_repository_arns: Sequence[str]) -> dict:
    a = ctx.arn
    return doc([
        allow("Models", ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
              [f"arn:{ctx.partition}:bedrock:*::foundation-model/*", a("bedrock", "inference-profile/*"),
               a("bedrock", "project/default")]),
        allow("Guardrail", "bedrock:ApplyGuardrail", a("bedrock", "guardrail/*"),
              {"StringEquals": {"aws:ResourceTag/Project": PROJECT}, "StringLike": {"aws:ResourceTag/Stage": "mr-*"}}),
        allow("Runtime", ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
              [a("bedrock-agentcore", "runtime/m11_mr*"), a("bedrock-agentcore", "runtime/m11_mr*/runtime-endpoint/*")]),
        allow("Bundle", "bedrock-agentcore:GetConfigurationBundleVersion",
              a("bedrock-agentcore", "configuration-bundle/m11_mr*")),
        allow("Memory", ["bedrock-agentcore:BatchCreateMemoryRecords", "bedrock-agentcore:ListMemoryRecords"],
              a("bedrock-agentcore", "memory/m11_mr*")),
        allow("Evaluate", "bedrock-agentcore:Evaluate",
              [a("bedrock-agentcore", "evaluator/Builtin.*"), "arn:aws:bedrock-agentcore:::evaluator/Builtin.*",
               a("bedrock-agentcore", "evaluator/m11_mr*")]),
        allow("Lambda", "lambda:InvokeFunction", a("lambda", "function:m11-mr*")),
        allow("Tables", ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                         "dynamodb:Query"], a("dynamodb", "table/m11-mr*")),
        allow("Module10Profiles", "dynamodb:GetItem", a("dynamodb", f"table/{ctx.state_table}")),
        allow("Objects", ["s3:GetObject", "s3:PutObject"], ctx.s3("m11-mr*/*")),
        allow("ReleaseData", "s3:GetObject", [ctx.s3(ctx.release_bucket, p) for p in
                                              ("evidence/*", "snapshots/*", "handlers/*")]),
        allow("LogsWrite", ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
              [a("logs", "log-group:/aws/lambda/m11-mr*"), a("logs", "log-group:/aws/bedrock-agentcore/runtimes/m11_mr*")]),
        # AgentCore creates the runtime's -DEFAULT group with the runtime role, and without it exports no spans.
        allow("RuntimeLogGroups", "logs:CreateLogGroup", a("logs", "log-group:/aws/bedrock-agentcore/runtimes/m11_mr*")),
        allow("LogsRead", ["logs:FilterLogEvents", "logs:StartQuery", "logs:GetQueryResults"],
              [a("logs", "log-group:aws/spans:*"), a("logs", "log-group:/aws/bedrock-agentcore/runtimes/m11_mr*")]),
        allow("NoResourceType", ["logs:PutResourcePolicy", "logs:DescribeLogGroups", "xray:PutTraceSegments",
                                 "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets",
                                 "ecr:GetAuthorizationToken"], "*"),
        allow("Images", ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], list(image_repository_arns)),
        allow("Metrics", "cloudwatch:PutMetricData", "*",
              {"StringLike": {"cloudwatch:namespace": ["m11/*", "bedrock-agentcore"]}}),
        allow("WorkloadIdentity", ["bedrock-agentcore:GetWorkloadAccessToken",
                                   "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
              [a("bedrock-agentcore", "workload-identity-directory/default"),
               a("bedrock-agentcore", "workload-identity-directory/default/workload-identity/m11_mr*")]),
    ])


# ---------------------------------------------------------------- CI StartBuild
def ci_start_policy(project_arn: str, allowed_names: Sequence[str]) -> dict:
    statements = [
        {"Sid": "StartPreview", "Effect": "Allow", "Action": "codebuild:StartBuild", "Resource": project_arn,
         "Condition": {"ForAllValues:StringEquals": {"codebuild:environment.environmentVariables.name":
                                                     list(allowed_names)}}},
        {"Sid": "PollPreview", "Effect": "Allow", "Action": "codebuild:BatchGetBuilds", "Resource": project_arn},
    ]
    for n, key in enumerate(CI_DENY_KEYS, start=1):
        statements.append({"Sid": f"DenyOverride{n:02d}", "Effect": "Deny", "Action": "codebuild:StartBuild",
                           "Resource": "*", "Condition": {"Null": {key: "false"}}})
    return doc(statements)
