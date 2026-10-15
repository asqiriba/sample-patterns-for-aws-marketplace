# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The m11-partners program under Pulumi mocks:
every resource and rule, no ProjectAccessToken anywhere, and the `--pipeline-only` target list."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pulumi = pytest.importorskip("pulumi")
pytest.importorskip("pulumi_gitlab")
pytest.importorskip("pulumiverse_vercel")

from module11.infrastructure.partners import gitlab_project as GP  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
USERS = {"harness-owners": [{"user_id": 11}], "platform": [{"user_id": 11}], "quality": [{"user_id": 22}]}
# Group IDs where the bot can see the shared groups; quality is always named users (GP.USER_ONLY).
GROUPS = {"harness-owners": [{"group_id": 101}], "platform": [{"group_id": 102}], "quality": [{"user_id": 22}]}


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources = []

    def new_resource(self, args):
        inputs = dict(args.inputs)
        if args.typ == "pulumi:pulumi:StackReference":
            return f"{args.name}-id", {"name": inputs.get("name"), "outputs": {
                "gitlabRoles": {"m11-gitlab-ci": "arn:aws:iam::111122223333:role/m11-gitlab-ci"},
                "oidcAudience": "sts.amazonaws.com"}}
        self.resources.append((args.typ, args.name, inputs, None, args.resource_id))
        out = dict(inputs)
        if args.typ.endswith("ProjectProtectionBypass"):
            out["secret"] = "generated-bypass"  # nosec B105 - a fake Pulumi output in the test's mock
        return f"{args.name}-id", out

    def call(self, args):
        if args.token == "aws:index/getRegion:getRegion":  # nosec B105 - a Pulumi invoke token (function name), not a credential
            return {"region": "us-east-1", "name": "us-east-1", "id": "us-east-1"}
        return {}

    def by_type(self, short):
        return {n: i for t, n, i, _, _ in self.resources if t.rsplit(":", 1)[-1] == short}


def run_program(approvers=USERS, import_project=False):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="m11-partners", stack="shared", preview=False)
    pulumi.runtime.set_all_config({
        "m11-partners:gitlabProject": "example-group/devops-companion-harness",
        "m11-partners:gitlabProjectId": "12345678", "m11-partners:botUserId": "43052940",
        "m11-partners:botLogin": "project_12345678_bot_x", "m11-partners:vercelTeamId": "team_EXAMPLE",
        "m11-partners:vercelTeamSlug": "example-team",
        "m11-partners:approvers": json.dumps(approvers), "m11-partners:importProject": str(import_project).lower()})

    @pulumi.runtime.test
    def body():
        import runpy
        runpy.run_path(str(ROOT / "module11/infrastructure/partners/__main__.py"), run_name="__main__")
        return pulumi.Output.from_input(True)
    body()
    return mocks


def test_every_governance_resource_and_no_project_access_token():
    mocks = run_program()
    types = {t.rsplit(":", 1)[-1] for t, *_ in mocks.resources}
    assert "ProjectAccessToken" not in types and "GroupAccessToken" not in types        # the bot uses a project token
    project = mocks.by_type("Project")["project"]
    assert project["ciIdTokenSubClaimComponents"] == ["project_path", "ref_type", "ref", "environment_protected"]
    assert project["onlyAllowMergeIfPipelineSucceeds"] is True and project["allowMergeOnSkippedPipeline"] is False
    tag = mocks.by_type("TagProtection")["release-tags"]
    assert tag["tag"] == "harness/v*" and tag["createAccessLevel"] == "no one"
    assert tag["allowedToCreates"] == [{"userId": 43052940}]
    main = mocks.by_type("BranchProtection")["main-protection"]
    assert main["allowedToPushes"] == [{"accessLevel": "no one"}] and main["codeOwnerApprovalRequired"] is True
    assert main["allowedToMerges"] == [{"accessLevel": "maintainer"}] and "pushAccessLevel" not in main
    assert main["allowForcePush"] is False
    rules_ = mocks.by_type("ProjectPushRules")["push-rules"]
    assert rules_["rejectUnsignedCommits"] is True and rules_["preventSecrets"] is True
    assert mocks.by_type("ProjectLevelMrApprovals")["mr-approvals"]["mergeRequestsAuthorApproval"] is False
    any_rule = mocks.by_type("ProjectApprovalRule")["any-approver"]
    assert any_rule["ruleType"] == "any_approver" and any_rule["approvalsRequired"] == 1
    assert mocks.by_type("PipelineSchedule")["cleanup-schedule"]["description"] == "cleanup:previews"
    assert set(mocks.by_type("ProjectLabel")) == {f"label-{l.replace('::', '-')}" for l in GP.LABELS}
    variables = {i["key"]: i for i in mocks.by_type("ProjectVariable").values()}
    assert set(variables) == {"AWS_REGION", "M11_OIDC_AUD", "M11_GITLAB_CI_ROLE_ARN", "M11_GITLAB_CI_INFRA_ROLE_ARN",
                              "M11_PROMOTER_ROLE_ARN"}
    assert all(v["masked"] is False and v["protected"] is False for v in variables.values())
    assert variables["M11_PROMOTER_ROLE_ARN"]["value"].startswith("pending-")          # until the pipeline exists


def test_protected_environments_user_form():
    envs = run_program(USERS).by_type("ProjectProtectedEnvironment")
    assert envs["protected-env-production"]["approvalRules"] == [{"userId": 11}]
    assert envs["protected-env-production-gated"]["approvalRules"] == [{"userId": 11}, {"userId": 22}]
    assert envs["protected-env-production-gated"]["deployAccessLevelsAttributes"] == [{"userId": 11}, {"userId": 22}]
    assert envs["protected-env-review-infra"]["approvalRules"] == [{"userId": 11}]


def test_protected_environments_group_form():
    envs = run_program(GROUPS).by_type("ProjectProtectedEnvironment")
    assert envs["protected-env-production"]["approvalRules"] == [{"groupId": 101, "requiredApprovals": 1}]
    assert envs["protected-env-production-gated"]["approvalRules"] == [
        {"groupId": 101, "requiredApprovals": 1}, {"userId": 22}]
    assert envs["protected-env-review-infra"]["deployAccessLevelsAttributes"] == [{"groupId": 102}]


def test_quality_approvers_must_be_users_never_a_group_rule():
    """Apply after the 10/13 recording: a group rule can count an Owner inherited from the parent group, so the
    quality approval of production-gated names users only."""
    settings = GP.Settings(project_id="1", project_name="p", bot_user_id=2,
                           approvers={**GROUPS, "quality": [{"group_id": 103}]})
    with pytest.raises(ValueError, match="quality approvers must be users"):
        GP.approval_rules(settings, "production-gated")
    assert GP.approval_rules(settings, "production") == [{"group_id": 101, "required_approvals": 1}]


def test_import_run_sets_only_the_name_and_keeps_the_project():
    mocks = run_program(import_project=True)
    rows = [(n, i, imp) for t, n, i, _, imp in mocks.resources if t == "gitlab:index/project:Project"]
    name, inputs, import_id = rows[0]
    assert import_id == "12345678" and inputs == {"name": "devops-companion-harness"}


def test_vercel_project_and_bypass_secret():
    mocks = run_program()
    project = mocks.by_type("Project")["ui-project"]
    assert project["buildCommand"] == "node module11/vercel/build.mjs"
    assert project["outputDirectory"] == "module11/vercel/dist" and project["teamId"] == "team_EXAMPLE"
    assert project["gitRepository"] == {"type": "gitlab", "repo": "example-group/devops-companion-harness"}
    assert "rootDirectory" not in project                                               # the repository root
    assert project["automaticallyExposeSystemEnvironmentVariables"] is True             # VERCEL_BRANCH_URL
    secret = mocks.by_type("Secret")["vercel-bypass-secret"]
    assert secret["name"] == "devops-companion/partners/vercel-bypass"
    assert set(secret["tags"]) == {"Project", "Stage"}
    assert mocks.by_type("ProjectEnvironmentVariable")["ui-bot-login"]["key"] == "M11_BOT_LOGIN"


def test_pipeline_only_targets_the_first_seed_set():
    keys = ["AWS_REGION", "M11_OIDC_AUD", "M11_GITLAB_CI_ROLE_ARN", "M11_GITLAB_CI_INFRA_ROLE_ARN",
            "M11_PROMOTER_ROLE_ARN"]
    urns = GP.pipeline_only_urns("shared", keys)
    names = {u.rsplit("::", 1)[-1] for u in urns}
    assert names == {"project", "release-tags", "env-production", "env-production-gated", "env-review-infra",
                     "protected-env-production", "protected-env-production-gated", "protected-env-review-infra",
                     *(GP.variable_name(k) for k in keys)}
    assert not names & {"main-protection", "push-rules", "mr-approvals", "any-approver", "cleanup-schedule"}
    assert all(u.startswith("urn:pulumi:shared::m11-partners::gitlab:index/") for u in urns)
    registered = {n for _, n, *_ in run_program().resources}
    assert names <= registered                                   # the targets are the program's own URNs


def test_missing_approver_is_an_error():
    with pytest.raises(ValueError):
        GP._principals(GP.Settings("1", "p", 1, {"harness-owners": []}), ["harness-owners"])
