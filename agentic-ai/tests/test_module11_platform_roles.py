# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-platform's IAM renderers: every rendered policy passes P1 and fits IAM's
size limits; the operator trust template; the CI StartBuild policy shape; the P1d positions the
deployer policies rely on (generated IDs, create-time tagging, mr-* keys, latch reads)."""
import json

import pytest

from module11.infrastructure.platform import roles as R
from module11.infrastructure.policy import rules as P
from module11.release import iam_cover

A = "111122223333"
CTX = R.Ctx(A, "us-east-1", f"arn:aws:kms:us-east-1:{A}:key/main", f"arn:aws:kms:us-east-1:{A}:key/preview",
            "us-east-1_EXAMPLE", "request-state-abc")
OPS = [f"arn:aws:iam::{A}:user/operator"]
DEPLOYER = {"M11Role": "deployer"}


@pytest.mark.parametrize("stage", ["dev", "staging", "prod"])
def test_stage_deployer_policies_pass_p1_and_fit(stage):
    docs = R.deployer_policies(CTX, stage)
    assert sorted(docs) == [f"m11-{stage}-deployer-{p}" for p in ("agentcore", "compute", "edge", "ops", "state")]
    ops = docs[f"m11-{stage}-deployer-ops"]["Statement"]
    esm = next(s for s in ops if s["Sid"] == "EventSourceMappings")
    assert esm["Condition"]["ArnLike"]["lambda:FunctionArn"].endswith(f":function:m11-{stage}-*")
    for name, doc in docs.items():
        assert P.p1(doc, policy_tags=DEPLOYER, stagekey=stage, role_name=f"m11-{stage}-deployer") == [], name
        assert R.chars(doc) <= R.MANAGED_POLICY_LIMIT


def test_preview_deployer_and_boundary_pass_p1():
    for name, doc in R.preview_deployer_policies(CTX).items():
        assert P.p1(doc, policy_tags=DEPLOYER, stagekey="mr", stage="mr", role_name="m11-preview-deployer") == [], name
    boundary = R.mr_boundary(CTX, [f"arn:aws:ecr:us-east-1:{A}:repository/m11/devops-companion"])
    assert P.p1(boundary, policy_tags={"M11Role": "boundary"}, stagekey="mr", stage="mr") == []
    assert R.chars(boundary) <= R.MANAGED_POLICY_LIMIT
    image = R.image_build_policy(CTX, f"arn:aws:ecr:us-east-1:{A}:repository/m11/devops-companion",
                                 "/aws/codebuild/m11-image")
    assert P.p1(image, policy_tags={"M11Role": "build"}, stagekey="shared") == []


def test_only_cognito_stages_get_browser_client_permissions():
    def actions(doc):
        return {a for s in doc["Statement"] for a in iam_cover.as_list(s.get("Action"))}
    assert "cognito-idp:CreateUserPoolClient" in actions(R.deployer_policies(CTX, "prod")["m11-prod-deployer-edge"])
    assert "cognito-idp:CreateUserPoolClient" not in actions(R.deployer_policies(CTX, "dev")["m11-dev-deployer-edge"])
    assert "cognito-idp:CreateUserPoolClient" in actions(R.preview_deployer_policies(CTX)["m11-preview-deployer-edge"])


def test_staging_deployer_cannot_touch_deployers_or_prod_state():
    compute = R.deployer_policies(CTX, "staging")["m11-staging-deployer-compute"]
    deny = next(s for s in compute["Statement"] if s["Sid"] == "DenyDeployerRoles")
    assert deny["Effect"] == "Deny" and "iam:PutRolePolicy" in deny["Action"]
    state = R.deployer_policies(CTX, "staging")["m11-staging-deployer-state"]
    objects = next(s for s in state["Statement"] if s["Sid"] == "StateObjects")["Resource"]
    assert all("/m11-harness/staging" in r for r in objects)        # never another stage's state or secrets


def test_preview_deployer_creates_roles_only_with_the_boundary():
    compute = R.preview_deployer_policies(CTX)["m11-preview-deployer-compute"]
    create = next(s for s in compute["Statement"] if s["Sid"] == "RolesWithBoundary")
    assert "iam:CreateRole" in create["Action"]
    assert create["Condition"] == {"StringEquals": {"iam:PermissionsBoundary": CTX.boundary_arn}}
    state = R.preview_deployer_policies(CTX)["m11-preview-deployer-state"]
    assert all(CTX.preview_state_bucket in json.dumps(s) or s["Sid"] not in {"StateList", "StateObjects"}
               for s in state["Statement"])


def test_operator_trust_template():
    trust = R.role_trust(OPS, [f"arn:aws:iam::{A}:role/m11-preview-deployer"])
    assert P.operator_trust("m11-dev-deployer", trust, OPS) == []
    assert R.role_trust([]) == {"Version": "2012-10-17", "Statement": []}


def test_ci_start_policy_shape():
    arn = f"arn:aws:codebuild:us-east-1:{A}:project/m11-preview"
    doc = R.ci_start_policy(arn, ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA"])
    assert P.is_ci_start_policy(doc, arn, ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA"])
    assert len([s for s in doc["Statement"] if s["Effect"] == "Deny"]) == 21
    assert R.chars(doc) < R.INLINE_LIMIT


# ---------------------------------------------------------------- P1d positions used by the deployers
def _ok(arn, statement, *, tags=DEPLOYER, stage="dev", role="m11-dev-deployer"):
    return iam_cover.resource_wildcard_ok(arn, statement, tags, stage, stage=stage, role_name=role, policy=[statement])


def test_create_time_agentcore_wildcards_need_request_tags():
    arn = f"arn:aws:bedrock-agentcore:us-east-1:{A}:runtime/*"
    tagged = {"Action": ["bedrock-agentcore:CreateAgentRuntimeEndpoint", "bedrock-agentcore:TagResource"],
              "Condition": {"StringEquals": {"aws:RequestTag/Stage": "dev", "aws:RequestTag/Project": "m11-devops-companion"}}}
    assert _ok(arn, tagged)
    assert not _ok(arn, {**tagged, "Condition": {}})
    assert not _ok(arn, {**tagged, "Action": ["bedrock-agentcore:DeleteAgentRuntime"]})
    assert not _ok(arn, tagged, tags={"M11Role": "tool"})
    assert not _ok(arn, {**tagged, "Condition": {"StringEquals": {"aws:RequestTag/Stage": "prod",
                                                                  "aws:RequestTag/Project": "m11-devops-companion"}}})


def test_read_only_wildcards_for_deployers():
    assert _ok(f"arn:aws:bedrock-agentcore:us-east-1:{A}:configuration-bundle/*",
               {"Action": ["bedrock-agentcore:ListConfigurationBundleVersions"]})
    deleted = {"Action": ["apigateway:GET"], "Condition": {"Null": {"aws:ResourceTag/Stage": "true"}}}
    assert _ok("arn:aws:apigateway:us-east-1::/restapis/*", deleted)
    assert not _ok("arn:aws:apigateway:us-east-1::/restapis/*", {**deleted, "Action": ["apigateway:DELETE"]})
    latch = f"arn:aws:ssm:us-east-1:{A}:parameter/m11/devops-companion/latch/*"
    assert _ok(latch, {"Action": ["ssm:GetParameter"]})
    assert not _ok(latch, {"Action": ["ssm:PutParameter"]})
    assert _ok(latch, {"Action": ["ssm:PutParameter"]}, tags={"M11Role": "controller"})


def test_mr_keys_and_parameters_only_for_preview_deployers():
    param = f"arn:aws:ssm:us-east-1:{A}:parameter/m11/devops-companion/mr-*"
    key = f"arn:aws:s3:::m11-release-{A}-us-east-1/ledger/mr-*"
    for arn in (param, key):
        assert _ok(arn, {"Action": ["ssm:GetParameter"]}, stage="mr", role="m11-preview-deployer")
        assert not _ok(arn, {"Action": ["ssm:GetParameter"]}, stage="dev", role="m11-dev-deployer")


def test_boundary_may_name_any_foundation_model():
    stmt = {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": ["arn:aws:bedrock:*::foundation-model/*"]}
    assert P.p1c([stmt], policy_tags={"M11Role": "boundary"}) == []
    assert P.p1c([stmt], policy_tags={"M11Role": "runtime"})


@pytest.mark.parametrize("stage", ["dev", "staging", "prod"])
def test_deployers_may_create_the_code_based_evaluator(stage):
    """CreateEvaluator has no resource type (like CreateAgentRuntime), so it sits with the create actions on
    `*`; managing the evaluator afterwards stays scoped to the stage's own names."""
    docs = R.deployer_policies(CTX, stage)
    text = json.dumps(docs)
    assert "bedrock-agentcore:CreateEvaluator" in json.dumps(docs[f"m11-{stage}-deployer-state"]) or \
        "bedrock-agentcore:CreateEvaluator" in text
    scoped = [s for d in docs.values() for s in d["Statement"] if s.get("Sid") == "Evaluators"]
    assert scoped and scoped[0]["Resource"] == f"arn:aws:bedrock-agentcore:us-east-1:{A}:evaluator/m11_{stage}_*"
    preview = json.dumps(R.preview_deployer_policies(CTX))
    assert "bedrock-agentcore:CreateEvaluator" in preview


def test_builtin_evaluator_arns_in_both_forms_are_for_evaluate_only():
    """IAM authorizes Evaluate on `<region>:<account>:evaluator/Builtin.X` (seen live), so the eval runner and the
    boundary list that form next to the account-less one; neither may carry any other action."""
    for arn in ("arn:aws:bedrock-agentcore:::evaluator/Builtin.*", f"arn:aws:bedrock-agentcore:us-east-1:{A}:evaluator/Builtin.*"):
        assert _ok(arn, {"Action": "bedrock-agentcore:Evaluate"})
        assert not _ok(arn, {"Action": ["bedrock-agentcore:Evaluate", "bedrock-agentcore:DeleteEvaluator"]})
    boundary = R.mr_boundary(CTX, [f"arn:aws:ecr:us-east-1:{A}:repository/m11/devops-companion"])
    evaluate = next(s for s in boundary["Statement"] if s["Sid"] == "Evaluate")
    assert f"arn:aws:bedrock-agentcore:us-east-1:{A}:evaluator/Builtin.*" in evaluate["Resource"]
