# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CrossGuard rule functions P1-P11 called directly. Importable without
pulumi_policy. The "every policy the design renders passes" cases and test_ci_start_policy on the
rendered roles.ci_start_policy() live with the role renderers' tests; here
the same shapes are built by hand."""
import copy
import sys

import pytest

from module11.infrastructure.policy import rules as P

R, A = "us-east-1", "111122223333"
P_ = f"{R}:{A}"
DEPLOYER = {"M11Role": "deployer"}
BOUNDARY = {"M11Role": "boundary"}
CONTROLLER = {"M11Role": "controller"}
TOOL = {"M11Role": "tool"}


def stmt(action, resource, condition=None, sid="S", effect="Allow"):
    s = {"Sid": sid, "Effect": effect, "Action": action, "Resource": resource}
    if condition:
        s["Condition"] = condition
    return s


def p1d(statements, tags, stagekey="prod", role=None, stage=None):
    return P.p1d(statements, policy_tags=tags, stagekey=stagekey, stage=stage, role_name=role)


def test_importable_without_pulumi_policy():
    import importlib
    saved = sys.modules.get("pulumi_policy")
    sys.modules["pulumi_policy"] = None
    try:
        importlib.reload(P)
    finally:
        if saved is None:
            sys.modules.pop("pulumi_policy", None)
        else:
            sys.modules["pulumi_policy"] = saved


# ---------------------------------------------------------------- P1a-P1c
def test_p1a_rejects_wildcard_actions():
    assert P.p1a([stmt("s3:Get*", "arn:aws:s3:::m11-x/evidence/*")])
    assert P.p1a([stmt("s3:*", "*")])
    assert P.p1a([stmt("s3:GetObject", "arn:aws:s3:::m11-x/evidence/*")]) == []


def test_p1b_resource_star():
    assert P.p1b([stmt("logs:PutResourcePolicy", "*")]) == []
    assert P.p1b([stmt("s3:GetObject", "*")])
    assert P.p1b([stmt("cloudwatch:PutMetricData", "*")])
    assert P.p1b([stmt("cloudwatch:PutMetricData", "*", {"StringEquals": {"cloudwatch:namespace": "m11/Release"}})]) == []
    assert P.p1b([stmt("cloudwatch:PutMetricData", "*", {"StringEquals": {"cloudwatch:namespace": "AWS/Lambda"}})])


def test_p1b_create_guardrail_needs_request_tags_in_a_deployer():
    cond = {"StringEquals": {"aws:RequestTag/Stage": "staging", "aws:RequestTag/Project": "m11-devops-companion"},
            "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Stage"]}}
    s = stmt("bedrock:CreateGuardrail", "*", cond)
    assert P.p1b([s], policy_tags=DEPLOYER, role_name="m11-staging-deployer", stage="staging") == []
    assert P.p1b([s], policy_tags=TOOL, role_name="m11-staging-x", stage="staging")
    assert P.p1b([stmt("bedrock:CreateGuardrail", "*")], policy_tags=DEPLOYER, stage="staging")
    preview = {"StringLike": {"aws:RequestTag/Stage": "mr-*"},
               "StringEquals": {"aws:RequestTag/Project": "m11-devops-companion"},
               "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Stage"]}}
    assert P.p1b([stmt("bedrock:CreateGuardrail", "*", preview)], policy_tags=DEPLOYER, role_name="m11-preview") == []
    assert P.p1b([stmt("bedrock:CreateGuardrail", "*", preview)], policy_tags=DEPLOYER, role_name="m11-preview-infra")


def test_p1c_region_wildcards_only_for_invoke_model():
    assert P.p1c([stmt("bedrock:InvokeModel", "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-luna")]) == []
    assert P.p1c([stmt("bedrock:GetFoundationModel", "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-luna")])
    assert P.p1c([stmt("lambda:InvokeFunction", "arn:aws:lambda:*:111122223333:function:m11-prod-x")])


# ---------------------------------------------------------------- P1d positions
def test_p1d_1_s3_objects():
    assert p1d([stmt("s3:GetObject", f"arn:aws:s3:::m11-release-{A}/evidence/*")], TOOL) == []
    assert p1d([stmt("s3:GetObject", "arn:aws:s3:::*/x")], TOOL)
    assert p1d([stmt("s3:GetObject", "arn:aws:s3:::m11-*/x")], TOOL)
    assert p1d([stmt("s3:GetObject", "arn:aws:s3:::m11-mr*/*")], BOUNDARY) == []
    assert p1d([stmt("s3:GetObject", "arn:aws:s3:::m11-x/ev*dence/*")], TOOL)


def test_p1d_1_ledger_prefix_is_the_stage_s_own():
    put = stmt("s3:PutObject", f"arn:aws:s3:::m11-release-{A}/ledger/prod/*")
    assert p1d([put], CONTROLLER) == []
    assert p1d([stmt("s3:PutObject", f"arn:aws:s3:::m11-release-{A}/ledger/*")], CONTROLLER)
    assert p1d([stmt("s3:PutObject", f"arn:aws:s3:::m11-release-{A}/ledger/staging/*")], CONTROLLER)
    get = stmt("s3:GetObject", f"arn:aws:s3:::m11-release-{A}/ledger/prod/*")
    listing = stmt("s3:ListBucket", f"arn:aws:s3:::m11-release-{A}", {"StringLike": {"s3:prefix": "ledger/prod/*"}})
    assert p1d([get, listing], {"M11Role": "drift"}) == []


def test_p1d_2_log_stream_suffix():
    assert p1d([stmt("logs:FilterLogEvents", f"arn:aws:logs:{P_}:log-group:aws/spans:*")], TOOL) == []
    assert p1d([stmt("logs:FilterLogEvents", f"arn:aws:logs:{P_}:log-group:*")], TOOL)


def test_p1d_3_latch_only_in_the_controller():
    latch = stmt("ssm:PutParameter", f"arn:aws:ssm:{P_}:parameter/m11/devops-companion/latch/*")
    assert p1d([latch], CONTROLLER) == []
    assert p1d([latch], TOOL)


def test_p1d_4_name_prefixes():
    own = stmt("lambda:InvokeFunction", f"arn:aws:lambda:{P_}:function:m11-prod-*")
    assert p1d([own], TOOL, stagekey="prod") == []
    assert p1d([own], TOOL, stagekey="staging")
    assert p1d([stmt("bedrock-agentcore:GetBatchEvaluation", f"arn:aws:bedrock-agentcore:{P_}:batch-evaluate/m11_prod_shadow*")],
               CONTROLLER) == []
    broad = stmt("bedrock-agentcore:InvokeAgentRuntime", f"arn:aws:bedrock-agentcore:{P_}:runtime/m11_mr*")
    assert p1d([broad], BOUNDARY) == []
    assert p1d([broad], TOOL)
    assert p1d([stmt("iam:PassRole", f"arn:aws:iam::{A}:role/m11-*")], DEPLOYER) == []


def test_p1d_5_generated_ids():
    assert p1d([stmt("bedrock-agentcore:Evaluate", "arn:aws:bedrock-agentcore:::evaluator/Builtin.*")], TOOL) == []
    assert p1d([stmt("bedrock-agentcore:GetEvaluator", "arn:aws:bedrock-agentcore:::evaluator/Builtin.*")], TOOL)
    tagged = {"StringEquals": {"aws:ResourceTag/Project": "m11-devops-companion"},
              "StringLike": {"aws:ResourceTag/Stage": "mr-*"}}
    assert p1d([stmt("bedrock:ApplyGuardrail", f"arn:aws:bedrock:{P_}:guardrail/*", tagged)], BOUNDARY) == []
    assert p1d([stmt("bedrock:ApplyGuardrail", f"arn:aws:bedrock:{P_}:guardrail/*")], BOUNDARY)
    assert p1d([stmt("bedrock:ApplyGuardrail", f"arn:aws:bedrock:{P_}:guardrail/*", tagged)], TOOL)
    assert p1d([stmt("bedrock:InvokeModel", f"arn:aws:bedrock:{P_}:inference-profile/*")], BOUNDARY) == []
    assert p1d([stmt("bedrock:InvokeModel", f"arn:aws:bedrock:{P_}:inference-profile/*")], TOOL)


# ---------------------------------------------------------------- P1d (6): deployer templates
def deployer_policy(stage="staging", preview=False):
    eq, neq = ("StringLike", "StringNotLike") if preview else ("StringEquals", "StringNotEquals")
    value = "mr-*" if preview else stage
    rest = [f"arn:aws:apigateway:{R}::/{k}/*" for k in ("restapis", "usageplans", "apikeys")]
    guard = f"arn:aws:bedrock:{P_}:guardrail/*"
    req = {eq: {"aws:RequestTag/Stage": value}, "StringEquals": {"aws:RequestTag/Project": "m11-devops-companion"},
           "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Stage"]}}
    if not preview:
        req = {"StringEquals": {"aws:RequestTag/Stage": value, "aws:RequestTag/Project": "m11-devops-companion"},
               "ForAllValues:StringEquals": {"aws:TagKeys": ["Project", "Stage"]}}
    res = {eq: {"aws:ResourceTag/Stage": value}}
    return [
        stmt("apigateway:POST", [f"arn:aws:apigateway:{R}::/restapis", f"arn:aws:apigateway:{R}::/usageplans"], req, "Create"),
        stmt("bedrock:CreateGuardrail", "*", req, "CreateGuardrail"),
        stmt("bedrock:TagResource", guard, req, "CreateTagGuardrail"),
        stmt(["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"], rest, res, "Manage"),
        stmt(["bedrock:GetGuardrail", "bedrock:UpdateGuardrail", "bedrock:CreateGuardrailVersion", "bedrock:DeleteGuardrail"],
             guard, res, "ManageGuardrail"),
        stmt(["apigateway:GET", "apigateway:PUT", "apigateway:DELETE"], f"arn:aws:apigateway:{R}::/tags/*", None, "Tags"),
        stmt(["bedrock:TagResource", "bedrock:UntagResource"], guard, res, "TagOwnGuardrail"),
        stmt(["apigateway:PUT", "apigateway:DELETE", "bedrock:TagResource", "bedrock:UntagResource"],
             [f"arn:aws:apigateway:{R}::/tags/*", guard],
             {"ForAnyValue:StringEquals": {"aws:TagKeys": ["Stage", "Project"]}, neq: {"aws:RequestTag/Stage": value}},
             "DenyForeignStageTagging", "Deny"),
        stmt("bedrock:TagResource", guard, {"Null": {"aws:ResourceTag/Stage": "false"}, neq: {"aws:ResourceTag/Stage": value}},
             "DenyRetagOtherStageGuardrail", "Deny"),
        stmt("bedrock:TagResource", guard, {"Null": {"aws:ResourceTag/Project": "false"},
                                            "StringNotEquals": {"aws:ResourceTag/Project": "m11-devops-companion"}},
             "DenyRetagForeignProjectGuardrail", "Deny"),
        stmt(["apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"], rest,
             {neq: {"aws:ResourceTag/Stage": value}}, "DenyForeignApi", "Deny"),
        stmt(["bedrock:UpdateGuardrail", "bedrock:CreateGuardrailVersion", "bedrock:DeleteGuardrail"], guard,
             {neq: {"aws:ResourceTag/Stage": value}}, "DenyForeignGuardrail", "Deny"),
    ]


def test_deployer_templates_pass():
    assert P.p1(deployer_policy(), policy_tags=DEPLOYER, stagekey="staging", role_name="m11-staging-deployer") == []
    assert P.p1(deployer_policy(preview=True), policy_tags=DEPLOYER, stagekey="mr", stage="mr",
                role_name="m11-preview") == []


def _drop(policy, sid):
    return [s for s in policy if s["Sid"] != sid]


def test_restapis_needs_the_stage_condition_a_deployer_and_the_explicit_deny():
    policy = deployer_policy()
    assert p1d(policy, DEPLOYER, "staging", "m11-staging-deployer") == []
    no_cond = copy.deepcopy(policy)
    del next(s for s in no_cond if s["Sid"] == "Manage")["Condition"]
    assert p1d(no_cond, DEPLOYER, "staging", "m11-staging-deployer")
    assert p1d(policy, {"M11Role": "runtime"}, "staging", "m11-staging-runtime")
    assert p1d(_drop(policy, "DenyForeignApi"), DEPLOYER, "staging", "m11-staging-deployer")


def test_preview_infra_gets_guardrail_manage_but_no_create_statements():
    infra = _drop(_drop(_drop(deployer_policy(preview=True), "Create"), "CreateGuardrail"), "CreateTagGuardrail")
    assert P.p1(infra, policy_tags=DEPLOYER, stagekey="mr", stage="mr", role_name="m11-preview-infra") == []
    with_create = deployer_policy(preview=True)
    assert p1d(with_create, DEPLOYER, "mr", "m11-preview-infra", stage="mr")


def test_tag_statement_shapes():
    policy = deployer_policy()
    assert p1d(_drop(policy, "DenyForeignStageTagging"), DEPLOYER, "staging", "m11-staging-deployer")
    assert p1d(_drop(policy, "DenyRetagOtherStageGuardrail"), DEPLOYER, "staging", "m11-staging-deployer")
    missing = copy.deepcopy(policy)
    del next(s for s in missing if s["Sid"] == "CreateTagGuardrail")["Condition"]["ForAllValues:StringEquals"]
    assert p1d(missing, DEPLOYER, "staging", "m11-staging-deployer")
    # Iteration 5's blanket tag-key Deny (no RequestTag/Stage condition) in place of DenyForeignStageTagging.
    blanket = _drop(policy, "DenyForeignStageTagging") + [stmt(
        ["bedrock:TagResource"], f"arn:aws:bedrock:{P_}:guardrail/*",
        {"ForAnyValue:StringEquals": {"aws:TagKeys": ["Stage", "Project"]}}, "DenyTagKeys", "Deny")]
    assert p1d(blanket, DEPLOYER, "staging", "m11-staging-deployer")


# ---------------------------------------------------------------- P2-P9
def test_p2_to_p9():
    assert P.p2({"name": "m11-prod-x", "publish": True}) == [] and P.p2({"name": "m11-prod-x"})
    assert P.p3("bucket", {"Project": "m11-devops-companion", "Stage": "prod"}) == [] and len(P.p3("b", {})) == 2
    assert P.p4({"GITLAB_TOKEN": "glpat-xyz"}) and P.p4({"GITLAB_TOKEN": "arn:aws:secretsmanager:x"}) == []
    assert P.p4({"LOG_LEVEL": "INFO"}) == []
    assert P.p5("/aws/lambda/x", 30) == [] and P.p5("/aws/lambda/x", None) and P.p5("/aws/lambda/x", 90)
    trust = {"Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": A}}}]}
    assert P.p6("r", trust) == []
    no_cond = copy.deepcopy(trust)
    del no_cond["Statement"][0]["Condition"]
    assert P.p6("r", no_cond)
    assert P.p7("prod", "PUBLIC", False) and P.p7("prod", "PUBLIC", True) == [] and P.p7("staging", "PUBLIC", False) == []
    assert P.p8("mr-12", "m11-mr12-runtime", f"arn:aws:iam::{A}:policy/m11-mr-boundary") == []
    assert P.p8("mr-12", "m11-mr12-runtime", None) and P.p8("prod", "m11-prod-runtime", None) == []
    retained = [f"arn:aws:lambda:{P_}:function:m11-prod-query_metrics:3"]
    out = [f"arn:aws:lambda:{P_}:function:m11-prod-query_metrics:4"]
    assert P.p9(retained + out, retained, out, False) == []
    assert P.p9([f"arn:aws:lambda:{P_}:function:m11-prod-query_metrics"], retained, out, False)
    assert P.p9([f"arn:aws:lambda:{P_}:function:m11-prod-query_metrics:2"], retained, out, False)
    assert P.p9([None], retained, out, True) == [] and P.p9([None], retained, out, False)


# ---------------------------------------------------------------- P10, P11
BUCKET = f"m11-pipeline-artifacts-{A}"


def test_p10_caches():
    good = [{"name": "m11-deploy", "cache": {"type": "S3", "location": f"{BUCKET}/cache/m11-deploy"}},
            {"name": "m11-preview-image", "cache": {"type": "NO_CACHE"}}, {"name": "m11-preview-infra"}]
    assert P.p10(good, BUCKET) == []
    shared = good + [{"name": "m11-gate", "cache": {"type": "S3", "location": f"{BUCKET}/cache/m11-deploy"}}]
    assert any("cache must be S3 at" in m or "shares" in m for m in P.p10(shared, BUCKET))
    untrusted = [{"name": "m11-preview-image", "cache": {"type": "S3", "location": f"{BUCKET}/cache/m11-preview-image"}}]
    assert P.p10(untrusted, BUCKET)
    policies = {"m11-deploy": [stmt("s3:PutObject", f"arn:aws:s3:::{BUCKET}/cache/m11-gate/*")]}
    assert P.p10(good, BUCKET, policies)
    assert P.p10(good, BUCKET, {"m11-deploy": [stmt("s3:PutObject", f"arn:aws:s3:::{BUCKET}/cache/m11-deploy/*")]}) == []


def ci_start_policy(project_arn, names):
    deny = [stmt("codebuild:StartBuild", "*", {"Null": {key: "false"}}, f"DenyOverride{i:02d}", "Deny")
            for i, key in enumerate(P.CI_DENY_KEYS, start=1)]
    return [stmt("codebuild:StartBuild", project_arn,
                 {"ForAllValues:StringEquals": {"codebuild:environment.environmentVariables.name": names}}, "Start"),
            stmt("codebuild:BatchGetBuilds", project_arn, None, "Poll"), *deny]


SEAL_EXP = f"arn:aws:codebuild:{P_}:project/m11-seal-experiment"
PIPELINE_PROJECTS = [f"arn:aws:codebuild:{P_}:project/m11-seal", f"arn:aws:codebuild:{P_}:project/m11-deploy"]


def _p11(policies):
    return P.p11(policies, SEAL_EXP, "m11-pipeline",
                 PIPELINE_PROJECTS, {"m11-seal": "CODEPIPELINE", "m11-seal-experiment": "NO_SOURCE"})


def test_ci_start_policy_shape():
    policy = ci_start_policy(f"arn:aws:codebuild:{P_}:project/m11-preview", ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA"])
    assert len(P.CI_DENY_KEYS) == 21
    assert P.is_ci_start_policy(policy, f"arn:aws:codebuild:{P_}:project/m11-preview",
                                ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA"])
    assert all(len(s["Condition"]["Null"]) == 1 for s in policy if s["Effect"] == "Deny")
    infra = ci_start_policy("arn:p", ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA", "PREVIEW_JOB_ID"])
    assert P.is_ci_start_policy(infra, "arn:p", ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA", "PREVIEW_JOB_ID"])
    merged = policy[:2] + [stmt("codebuild:StartBuild", "*", {"Null": {k: "false" for k in P.CI_DENY_KEYS}}, "D", "Deny")]
    assert not P.is_ci_start_policy(merged, f"arn:aws:codebuild:{P_}:project/m11-preview",
                                    ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA"])
    assert len(__import__("json").dumps(policy)) < 10240


def test_p11_who_may_start_the_experiment_build():
    pipeline = [stmt("codebuild:StartBuild", PIPELINE_PROJECTS)]
    base = {"m11-operator-experiment": ci_start_policy(SEAL_EXP, ["SOURCE_SHA", "EXPERIMENT_ID"]), "m11-pipeline": pipeline}
    assert _p11(base) == []
    assert _p11({**base, "m11-other": [stmt("codebuild:StartBuild", SEAL_EXP)]})
    assert _p11({**base, "m11-other": [stmt("codebuild:StartBuild", f"arn:aws:codebuild:{P_}:project/m11-*")]})
    assert _p11({**base, "m11-other": [stmt("codebuild:StartBuild", "*")]})
    assert _p11({**base, "m11-other": [{"Effect": "Allow", "Action": "codebuild:StartBuild",
                                        "NotResource": f"arn:aws:codebuild:{P_}:project/m11-seal"}]})
    assert _p11({**base, "m11-pipeline": [stmt("codebuild:StartBuild", f"arn:aws:codebuild:{P_}:project/m11-*")]})
    loose = {**base, "m11-operator-experiment": [stmt("codebuild:StartBuild", SEAL_EXP)]}
    assert _p11(loose)
    assert P.p11(base, SEAL_EXP, "m11-pipeline", PIPELINE_PROJECTS, {"m11-seal-experiment": "CODEPIPELINE"})


def test_operator_trust():
    ops = [f"arn:aws:iam::{A}:user/operator"]
    good = {"Statement": [
        {"Sid": "OperatorAssume", "Effect": "Allow", "Principal": {"AWS": ops}, "Action": "sts:AssumeRole",
         "Condition": {"StringLike": {"sts:RoleSessionName": "m11-operator-*"}, "Null": {"sts:SourceIdentity": "false"}}},
        {"Sid": "OperatorSourceIdentity", "Effect": "Allow", "Principal": {"AWS": ops}, "Action": "sts:SetSourceIdentity"},
        {"Sid": "CodeBuild", "Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    assert P.operator_trust("m11-staging-deployer", good, ops) == []
    extra = copy.deepcopy(good)
    extra["Statement"].append({"Sid": "Loose", "Effect": "Allow", "Principal": {"AWS": ops}, "Action": "sts:AssumeRole"})
    assert P.operator_trust("m11-staging-deployer", extra, ops)
    missing = {"Statement": good["Statement"][1:]}
    assert P.operator_trust("m11-staging-deployer", missing, ops)
    assert P.operator_trust("m11-prod-deployer", {"Statement": []}, []) == []


@pytest.mark.parametrize("key", ["noResource", "requiresCondition"])
def test_action_lists_file(key):
    assert key in P.action_lists()
