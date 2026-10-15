# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CodePipeline V2 `m11-release` and the GitLab CodeConnections connection.

The connection is gated on `createGitlabConnection` alone: it is created PENDING before the
pipeline so the one-time console approval can happen while the rest is built. A pipeline whose
Source uses a PENDING connection fails at Source, so `enablePipeline=true` waits until it reads
AVAILABLE. The Source is GitLab project `<gitlabProject>` (branch main) through that connection,
with the push trigger's filePaths `module10/**` and `module11/**`.

Stages (VariableCheck entry conditions SKIP a stage whose rule fails):
  Source -> Analyze -> Image (IMAGE_REBUILD EQ true) -> Seal (RELEASE_KIND NE noop)
  -> Staging (deploy, gate, prune; RELEASE_KIND MATCHES ^(deploy|rebaseline|evalset)$)
  -> Approval (freeze check, ReleaseApproval; RELEASE_KIND EQ deploy)
  -> ExtendedApproval (APPROVAL EQ required) -> Production (deploy, bake; RELEASE_KIND EQ deploy)
Execution mode QUEUED: a stage holds one execution at a time, so a later release waits behind one
in Production until Bake ends.
"""
from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

from module11.infrastructure.platform import pipeline_roles as PR

# Kept here, not in release/naming.py: naming.py is in the rollout simulator's closure.
GITLAB_CONNECTION = "m11-gitlab"
SOURCE_PATHS = ["module10/**", "module11/**"]


def connection(tags: dict) -> aws.codeconnections.Connection:
    """The GitLab connection (provider type GitLab); an admin approves it once in the console."""
    return aws.codeconnections.Connection("gitlab-connection", name=GITLAB_CONNECTION,
                                          provider_type="GitLab", tags=tags)


def _env(**values: str) -> str:
    return json.dumps([{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in values.items()])


def _build(name: str, project: str, run_order: int = 1, namespace: str | None = None, **env: str) -> dict:
    action = {"name": name, "category": "Build", "owner": "AWS", "provider": "CodeBuild", "version": "1",
              "input_artifacts": ["source"], "run_order": run_order,
              "configuration": {"ProjectName": project,
                                "EnvironmentVariables": _env(PIPELINE_EXECUTION_ID="#{codepipeline.PipelineExecutionId}",
                                                             **env)}}
    if namespace:
        action["namespace"] = namespace
    return action


def _when(name: str, variable: str, operator: str, value: str) -> dict:
    return {"condition": {"result": "SKIP", "rules": [{
        "name": name, "rule_type_id": {"category": "Rule", "owner": "AWS", "provider": "VariableCheck", "version": "1"},
        "configuration": {"Variable": variable, "Operator": operator, "Value": value}}]}}


def _approval(name: str, level: str) -> dict:
    return {"name": name, "category": "Approval", "owner": "AWS", "provider": "Manual", "version": "1", "run_order": 2
            if name == "ReleaseApproval" else 1,
            "configuration": {"CustomData": f"{level}: #{{Seal.RELEASE_TAG}} #{{Seal.MANIFEST_VERSION}} behavior "
                                            f"#{{Seal.BEHAVIOR_DIGEST}}; approval #{{Analyze.APPROVAL}}; gate report "
                                            "under evaluations/staging/ (released by promote:* in GitLab, or "
                                            "`module11 release approve --break-glass`)"}}


def stages(*, connection_arn: pulumi.Input[str], repository: str, branch: str = "main") -> list[dict]:
    seal = {"MANIFEST_VERSION": "#{Seal.MANIFEST_VERSION}", "RELEASE_TAG": "#{Seal.RELEASE_TAG}",
            "RELEASE_KIND": "#{Analyze.RELEASE_KIND}", "COMMIT": "#{Analyze.COMMIT}"}
    return [
        {"name": "Source", "actions": [{
            "name": "Source", "category": "Source", "owner": "AWS", "provider": "CodeStarSourceConnection",
            "version": "1", "output_artifacts": ["source"], "namespace": "SourceVariables",
            "configuration": {"ConnectionArn": connection_arn, "FullRepositoryId": repository, "BranchName": branch,
                              "OutputArtifactFormat": "CODEBUILD_CLONE_REF", "DetectChanges": "false"}}]},
        {"name": "Analyze", "actions": [_build("Analyze", "m11-analyze", namespace="Analyze")]},
        {"name": "Image", "before_entry": _when("ImageRebuild", "#{Analyze.IMAGE_REBUILD}", "EQ", "true"),
         "actions": [_build("Image", "m11-image", namespace="Image", CONTEXT_HASH="#{Analyze.IMAGE_CONTEXT}",
                            IMAGE_TAG="#{Analyze.IMAGE_CONTEXT}")]},
        {"name": "Seal", "before_entry": _when("NotNoop", "#{Analyze.RELEASE_KIND}", "NE", "noop"),
         "actions": [_build("Seal", "m11-seal", namespace="Seal", APPROVAL="#{Analyze.APPROVAL}")]},
        {"name": "Staging", "before_entry": _when("Deployable", "#{Analyze.RELEASE_KIND}", "MATCHES",
                                                  "^(deploy|rebaseline|evalset)$"),
         "actions": [_build("Deploy", "m11-deploy", 1, STAGE="staging", ACTION="deploy", **seal),
                     _build("Gate", "m11-gate", 2, EVAL_SCOPE="#{Analyze.EVAL_SCOPE}", **seal),
                     _build("Prune", "m11-deploy", 3, STAGE="staging", ACTION="prune", **seal)]},
        {"name": "Approval", "before_entry": _when("IsDeploy", "#{Analyze.RELEASE_KIND}", "EQ", "deploy"),
         "actions": [{"name": "FreezeCheck", "category": "Invoke", "owner": "AWS", "provider": "Lambda",
                      "version": "1", "run_order": 1,
                      "configuration": {"FunctionName": PR.FREEZE_FUNCTION,
                                        "UserParameters": "#{Seal.RELEASE_TAG}"}},
                     _approval("ReleaseApproval", "default")]},
        {"name": "ExtendedApproval", "before_entry": _when("Required", "#{Analyze.APPROVAL}", "EQ", "required"),
         "actions": [_approval("ExtendedReleaseApproval", "required")]},
        {"name": "Production", "before_entry": _when("IsDeploy", "#{Analyze.RELEASE_KIND}", "EQ", "deploy"),
         "actions": [_build("Deploy", "m11-deploy", 1, STAGE="prod", ACTION="deploy", **seal),
                     _build("Bake", "m11-bake", 2, **seal)]},
    ]


def pipeline(*, role: aws.iam.Role, bucket: aws.s3.Bucket, connection_arn: pulumi.Input[str], repository: str,
             tags: dict, depends_on: list) -> aws.codepipeline.Pipeline:
    return aws.codepipeline.Pipeline(
        "release-pipeline", name=PR.PIPELINE, role_arn=role.arn, pipeline_type="V2", execution_mode="QUEUED",
        artifact_stores=[{"location": bucket.bucket, "type": "S3"}],
        stages=stages(connection_arn=connection_arn, repository=repository),
        triggers=[{"provider_type": "CodeStarSourceConnection", "git_configuration": {
            "source_action_name": "Source",
            "pushes": [{"branches": {"includes": ["main"]}, "file_paths": {"includes": SOURCE_PATHS}}]}}],
        tags=tags, opts=pulumi.ResourceOptions(depends_on=depends_on))


