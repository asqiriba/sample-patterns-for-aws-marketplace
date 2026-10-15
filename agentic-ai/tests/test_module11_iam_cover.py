# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The statement-coverage matcher and its globbing. The boundary below mirrors the
mr-* boundary table; roles.py renders the real one with roles.mr_boundary() and add the concrete
role coverage and Module 10 table-ARN cases."""
import json

import pytest

from module11.release import iam_cover as I

R, A = "us-east-1", "111122223333"
P = f"{R}:{A}"
BOUNDARY = {"Version": "2012-10-17", "Statement": [
    {"Sid": "Models", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:{P}:inference-profile/*",
                  f"arn:aws:bedrock:{P}:project/default"]},
    {"Sid": "Guardrail", "Effect": "Allow", "Action": "bedrock:ApplyGuardrail", "Resource": f"arn:aws:bedrock:{P}:guardrail/*",
     "Condition": {"StringEquals": {"aws:ResourceTag/Project": "m11-devops-companion"},
                   "StringLike": {"aws:ResourceTag/Stage": "mr-*"}}},
    {"Sid": "Runtime", "Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:StopRuntimeSession"],
     "Resource": [f"arn:aws:bedrock-agentcore:{P}:runtime/m11_mr*", f"arn:aws:bedrock-agentcore:{P}:runtime/m11_mr*/runtime-endpoint/*"]},
    {"Sid": "Bundle", "Effect": "Allow", "Action": "bedrock-agentcore:GetConfigurationBundleVersion",
     "Resource": f"arn:aws:bedrock-agentcore:{P}:configuration-bundle/m11_mr*"},
    {"Sid": "Memory", "Effect": "Allow", "Action": ["bedrock-agentcore:BatchCreateMemoryRecords", "bedrock-agentcore:ListMemoryRecords"],
     "Resource": f"arn:aws:bedrock-agentcore:{P}:memory/m11_mr*"},
    {"Sid": "Evaluate", "Effect": "Allow", "Action": "bedrock-agentcore:Evaluate",
     "Resource": ["arn:aws:bedrock-agentcore:::evaluator/Builtin.*", f"arn:aws:bedrock-agentcore:{P}:evaluator/m11_mr*"]},
    {"Sid": "Lambda", "Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": f"arn:aws:lambda:{P}:function:m11-mr*"},
    {"Sid": "Tables", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                                                     "dynamodb:DeleteItem", "dynamodb:Query"],
     "Resource": f"arn:aws:dynamodb:{P}:table/m11-mr*"},
    {"Sid": "Module10Profiles", "Effect": "Allow", "Action": "dynamodb:GetItem",
     "Resource": f"arn:aws:dynamodb:{P}:table/request-state-0abc123"},
    {"Sid": "Objects", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": "arn:aws:s3:::m11-mr*/*"},
    {"Sid": "ReleaseData", "Effect": "Allow", "Action": "s3:GetObject",
     "Resource": [f"arn:aws:s3:::m11-release-{A}/evidence/*", f"arn:aws:s3:::m11-release-{A}/snapshots/*",
                  f"arn:aws:s3:::m11-release-{A}/handlers/*"]},
    {"Sid": "LogsWrite", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
     "Resource": [f"arn:aws:logs:{P}:log-group:/aws/lambda/m11-mr*:*", f"arn:aws:logs:{P}:log-group:/aws/bedrock-agentcore/runtimes/m11_mr*:*"]},
    {"Sid": "LogsRead", "Effect": "Allow", "Action": ["logs:FilterLogEvents", "logs:StartQuery", "logs:GetQueryResults"],
     "Resource": [f"arn:aws:logs:{P}:log-group:aws/spans:*", f"arn:aws:logs:{P}:log-group:/aws/bedrock-agentcore/runtimes/m11_mr*:*"]},
    {"Sid": "NoResourceType", "Effect": "Allow", "Action": ["logs:PutResourcePolicy", "xray:PutTraceSegments",
                                                             "xray:PutTelemetryRecords", "ecr:GetAuthorizationToken"],
     "Resource": "*"},
    {"Sid": "Images", "Effect": "Allow", "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
     "Resource": [f"arn:aws:ecr:{P}:repository/m11/devops-companion", f"arn:aws:ecr:{P}:repository/m11/devops-companion-preview"]},
    {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringLike": {"cloudwatch:namespace": ["m11/*", "bedrock-agentcore"]}}},
]}

# One covered statement per boundary Sid, and one uncovered variant of it.
CASES = {
    "Models": ({"Action": "bedrock:InvokeModel", "Resource": "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-luna"},
               {"Action": "bedrock:InvokeModel", "Resource": "arn:aws:bedrock:us-east-1:999999999999:inference-profile/x"}),
    "Guardrail": ({"Action": "bedrock:ApplyGuardrail", "Resource": f"arn:aws:bedrock:{P}:guardrail/abc",
                   "Condition": {"StringEquals": {"aws:ResourceTag/Project": "m11-devops-companion"},
                                 "StringLike": {"aws:ResourceTag/Stage": "mr-*"}}},
                  {"Action": "bedrock:ApplyGuardrail", "Resource": f"arn:aws:bedrock:{P}:guardrail/abc"}),
    "Runtime": ({"Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": f"arn:aws:bedrock-agentcore:{P}:runtime/m11_mr12-abc"},
                {"Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": f"arn:aws:bedrock-agentcore:{P}:runtime/m11_prod-abc"}),
    "Bundle": ({"Action": "bedrock-agentcore:GetConfigurationBundleVersion",
                "Resource": f"arn:aws:bedrock-agentcore:{P}:configuration-bundle/m11_mr12"},
               {"Action": "bedrock-agentcore:UpdateConfigurationBundle",
                "Resource": f"arn:aws:bedrock-agentcore:{P}:configuration-bundle/m11_mr12"}),
    "Memory": ({"Action": "bedrock-agentcore:ListMemoryRecords", "Resource": f"arn:aws:bedrock-agentcore:{P}:memory/m11_mr12-x"},
               {"Action": "bedrock-agentcore:DeleteMemoryRecord", "Resource": f"arn:aws:bedrock-agentcore:{P}:memory/m11_mr12-x"}),
    "Evaluate": ({"Action": "bedrock-agentcore:Evaluate", "Resource": "arn:aws:bedrock-agentcore:::evaluator/Builtin.*"},
                 {"Action": "bedrock-agentcore:Evaluate", "Resource": f"arn:aws:bedrock-agentcore:{P}:evaluator/m11_prod_x"}),
    "Lambda": ({"Action": "lambda:InvokeFunction", "Resource": f"arn:aws:lambda:{P}:function:m11-mr12-query_metrics:3"},
               {"Action": "lambda:InvokeFunction", "Resource": f"arn:aws:lambda:{P}:function:m11-prod-query_metrics:3"}),
    "Tables": ({"Action": ["dynamodb:GetItem", "dynamodb:Query"], "Resource": f"arn:aws:dynamodb:{P}:table/m11-mr12-control"},
               {"Action": "dynamodb:Scan", "Resource": f"arn:aws:dynamodb:{P}:table/m11-mr12-control"}),
    "Module10Profiles": ({"Action": "dynamodb:GetItem", "Resource": f"arn:aws:dynamodb:{P}:table/request-state-0abc123"},
                         {"Action": "dynamodb:PutItem", "Resource": f"arn:aws:dynamodb:{P}:table/request-state-0abc123"}),
    "Objects": ({"Action": "s3:PutObject", "Resource": "arn:aws:s3:::m11-mr12-artifacts/results/*"},
                {"Action": "s3:PutObject", "Resource": "arn:aws:s3:::m11-prod-artifacts/results/*"}),
    "ReleaseData": ({"Action": "s3:GetObject", "Resource": f"arn:aws:s3:::m11-release-{A}/evidence/*"},
                    {"Action": "s3:GetObject", "Resource": f"arn:aws:s3:::m11-release-{A}/ledger/prod/*"}),
    "LogsWrite": ({"Action": "logs:PutLogEvents", "Resource": f"arn:aws:logs:{P}:log-group:/aws/lambda/m11-mr12-x:*"},
                  {"Action": "logs:PutLogEvents", "Resource": f"arn:aws:logs:{P}:log-group:/aws/lambda/m11-prod-x:*"}),
    "LogsRead": ({"Action": "logs:StartQuery", "Resource": f"arn:aws:logs:{P}:log-group:aws/spans:*"},
                 {"Action": "logs:StartQuery", "Resource": f"arn:aws:logs:{P}:log-group:*"}),
    "NoResourceType": ({"Action": "xray:PutTraceSegments", "Resource": "*"},
                       {"Action": "iam:PassRole", "Resource": "*"}),
    "Images": ({"Action": "ecr:BatchGetImage", "Resource": f"arn:aws:ecr:{P}:repository/m11/devops-companion-preview"},
               {"Action": "ecr:PutImage", "Resource": f"arn:aws:ecr:{P}:repository/m11/devops-companion-preview"}),
    "Metrics": ({"Action": "cloudwatch:PutMetricData", "Resource": "*",
                 "Condition": {"StringLike": {"cloudwatch:namespace": ["m11/*"]}}},
                {"Action": "cloudwatch:PutMetricData", "Resource": "*"}),
}


def test_every_boundary_sid_has_a_case():
    assert sorted(CASES) == sorted(s["Sid"] for s in BOUNDARY["Statement"])


@pytest.mark.parametrize("sid", sorted(CASES))
def test_covered_and_uncovered_case_per_sid(sid):
    good, bad = CASES[sid]
    assert I.covers(BOUNDARY, {"Effect": "Allow", **good}) == []
    missing = I.covers(BOUNDARY, {"Effect": "Allow", **bad})
    assert missing and all(" " in m for m in missing)


def test_globbing_and_containment():
    assert I.pattern_contains("arn:x:function:m11-mr*", "arn:x:function:m11-mr12-*")
    assert not I.pattern_contains("arn:x:function:m11-mr12-*", "arn:x:function:m11-mr*")
    assert I.pattern_contains("s3:Get*", "s3:GetObject") and not I.pattern_contains("s3:GetObject", "s3:Get*")
    assert I.pattern_contains("*", "anything*") and I.pattern_contains("a?c", "abc") and not I.pattern_contains("a?c", "a*c")
    assert I.covers(BOUNDARY, {"Effect": "Allow", "Action": "S3:getobject",
                               "Resource": f"arn:aws:s3:::m11-release-{A}/handlers/*"}) == []
    assert I.covers(BOUNDARY, {"Effect": "Deny", "Action": "iam:*", "Resource": "*"}) == []


def test_condition_containment():
    outer = {"StringLike": {"cloudwatch:namespace": ["m11/*", "bedrock-agentcore"]}}
    assert I.condition_contains(outer, {"StringLike": {"cloudwatch:namespace": "m11/*"}, "Bool": {"x": "true"}})
    assert not I.condition_contains(outer, {"StringLike": {"cloudwatch:namespace": ["other"]}})
    assert not I.condition_contains(outer, None)
    assert I.condition_contains(None, None)


def test_grants_counts_not_action_and_not_resource():
    arn = f"arn:aws:codebuild:{P}:project/m11-seal-experiment"
    assert I.grants({"Effect": "Allow", "Action": "codebuild:StartBuild", "Resource": f"arn:aws:codebuild:{P}:project/m11-*"},
                    "codebuild:StartBuild", arn)
    assert I.grants({"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"}, "codebuild:StartBuild", arn)
    assert I.grants({"Effect": "Allow", "Action": "codebuild:*", "NotResource": f"arn:aws:codebuild:{P}:project/m11-seal"},
                    "codebuild:StartBuild", arn)
    assert not I.grants({"Effect": "Allow", "Action": "codebuild:StartBuild", "NotResource": arn},
                        "codebuild:StartBuild", arn)
    assert not I.grants({"Effect": "Deny", "Action": "*", "Resource": "*"}, "codebuild:StartBuild", arn)


def test_fixture_boundary_is_under_the_managed_policy_limit():
    assert len(json.dumps(BOUNDARY, separators=(",", ":"))) < 6144
