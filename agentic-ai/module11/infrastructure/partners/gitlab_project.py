# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The GitLab project's governance as Pulumi resources (pulumi-gitlab 10.3.1).

The project itself already exists (the user created it), so `Project` is imported once and kept on
destroy (`retain_on_delete`); teardown never deletes or archives it. Everything else is created here:
- protected tags `harness/v*` creatable only by the bot user (no access level may create them);
- protected environments `production` (1 harness-owners approval), `production-gated` (harness-owners
  and quality, 1 each) and `review-infra` (1 platform), whose approvers can also deploy [C-36];
- `ci_id_token_sub_claim_components` = [project_path, ref_type, ref, environment_protected];
- the non-secret CI variables the jobs read (role ARNs, region, OIDC audience);
- protected `main` (no direct push, code-owner approval), push rules (reject unsigned commits, prevent
  secrets), merge settings, author self-approval off, the any-approver rule, the MR labels and the nightly
  `cleanup:previews` schedule.
No ProjectAccessToken anywhere: the Ultimate trial allows one project token, the bot's.

`python -m module11 partners up --pipeline-only` applies only PIPELINE_ONLY (the first-seed order in
PARTNER_SETUP.md); the full `partners up` applies the rest.

Approvers are group IDs when the project's shared groups are visible, else user IDs (the user form):
`{"harness-owners": [{"group_id": 1}]}` or `{"quality": [{"user_id": 7}]}`. `quality` is always the
user form (USER_ONLY): an Owner inherited from the parent group can be eligible for a group rule, which would let
one person approve as both harness-owner and quality (apply after the 10/13 recording).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pulumi
import pulumi_gitlab as gitlab

SUB_CLAIM_COMPONENTS = ["project_path", "ref_type", "ref", "environment_protected"]
RELEASE_TAGS = "harness/v*"
ENVIRONMENTS = {
    "production": {"tier": "production", "approvers": ["harness-owners"]},
    "production-gated": {"tier": "production", "approvers": ["harness-owners", "quality"]},
    "review-infra": {"tier": "testing", "approvers": ["platform"]},
}
LABELS = {"m11-demo-run": "#6699cc", "change::retrieval-corpus": "#8fbc8f", "change::recommendation": "#d2b48c",
          "change::scenario": "#dda0dd"}
CLEANUP_CRON = "17 3 * * *"
USER_ONLY = frozenset({"quality"})
TYPES = {
    "project": "gitlab:index/project:Project",
    "tag": "gitlab:index/tagProtection:TagProtection",
    "environment": "gitlab:index/projectEnvironment:ProjectEnvironment",
    "protected-environment": "gitlab:index/projectProtectedEnvironment:ProjectProtectedEnvironment",
    "variable": "gitlab:index/projectVariable:ProjectVariable",
}


@dataclass(frozen=True)
class Settings:
    project_id: str
    project_name: str
    bot_user_id: int
    approvers: Mapping[str, Sequence[Mapping[str, int]]]
    variables: Mapping[str, Any] = field(default_factory=dict)
    import_project: bool = False


def variable_name(key: str) -> str:
    return f"var-{key.lower().replace('_', '-')}"


def pipeline_only_names(variable_keys: Sequence[str]) -> list[tuple[str, str]]:
    """(type, logical name) of what the pipeline and its tag pipelines need first."""
    out = [(TYPES["project"], "project"), (TYPES["tag"], "release-tags")]
    for env in ENVIRONMENTS:
        out += [(TYPES["environment"], f"env-{env}"), (TYPES["protected-environment"], f"protected-env-{env}")]
    out += [(TYPES["variable"], variable_name(k)) for k in sorted(variable_keys)]
    return out


def pipeline_only_urns(stack: str, variable_keys: Sequence[str], project: str = "m11-partners") -> list[str]:
    return [f"urn:pulumi:{stack}::{project}::{t}::{n}" for t, n in pipeline_only_names(variable_keys)]


def _principals(settings: Settings, groups: Sequence[str]) -> list[dict]:
    out = []
    for group in groups:
        entries = list(settings.approvers.get(group) or [])
        if not entries:
            raise ValueError(f"no approver for {group}: set group IDs or the user form")
        if group in USER_ONLY and any("user_id" not in e for e in entries):
            raise ValueError(f"{group} approvers must be users ({{\"user_id\": n}}): a group rule can count an "
                             "Owner inherited from the parent group")
        out += [dict(e) for e in entries]
    return out


def approval_rules(settings: Settings, env: str) -> list[dict]:
    """One rule per group, each requiring one approval (group_id or the user form)."""
    rules = []
    _principals(settings, ENVIRONMENTS[env]["approvers"])          # validates the form (quality: users only)
    for group in ENVIRONMENTS[env]["approvers"]:
        for entry in settings.approvers[group]:
            if "group_id" in entry:
                rules.append({**dict(entry), "required_approvals": 1})
                break               # one group rule covers the group
            # A user rule takes no required_approvals (that user's approval is the rule); the user form
            # names each group's one approver, so the rule count matches the group count.
            rules.append(dict(entry))
    return rules


def project(settings: Settings) -> gitlab.Project:
    """Imported once (inputs = the existing name only, so the import matches), then managed."""
    keep = pulumi.ResourceOptions(retain_on_delete=True, protect=False)
    if settings.import_project:
        return gitlab.Project("project", name=settings.project_name,
                              opts=pulumi.ResourceOptions.merge(keep, pulumi.ResourceOptions(import_=settings.project_id)))
    return gitlab.Project("project", name=settings.project_name, archive_on_destroy=True,
                          ci_id_token_sub_claim_components=SUB_CLAIM_COMPONENTS,
                          only_allow_merge_if_pipeline_succeeds=True, allow_merge_on_skipped_pipeline=False,
                          opts=keep)


def deploy(settings: Settings) -> dict[str, Any]:
    """Every GitLab resource; returns them by logical name (tests and the wrapper read the names)."""
    out: dict[str, Any] = {}
    proj = project(settings)
    out["project"] = proj
    pid = settings.project_id
    after = pulumi.ResourceOptions(depends_on=[proj])
    out["release-tags"] = gitlab.TagProtection("release-tags", project=pid, tag=RELEASE_TAGS, create_access_level="no one",
                                               allowed_to_creates=[{"user_id": settings.bot_user_id}], opts=after)
    for env, spec in ENVIRONMENTS.items():
        environment = gitlab.ProjectEnvironment(f"env-{env}", project=pid, name=env, tier=spec["tier"],
                                                stop_before_destroy=True, opts=after)
        out[f"env-{env}"] = environment
        out[f"protected-env-{env}"] = gitlab.ProjectProtectedEnvironment(
            f"protected-env-{env}", project=pid, environment=environment.name,
            approval_rules=approval_rules(settings, env),
            deploy_access_levels_attributes=_principals(settings, spec["approvers"]),
            opts=pulumi.ResourceOptions(depends_on=[environment]))
    for key, value in sorted(settings.variables.items()):
        out[variable_name(key)] = gitlab.ProjectVariable(variable_name(key), project=pid, key=key, value=value,
                                                         protected=False, masked=False, raw=True, opts=after)
    # ---- the MR governance (applied by the full `partners up`)
    # GitLab protects a new project's default branch (Maintainers push); `partners up` removes that default
    # protection right before this resource first creates the strict one. GitLab.com is an EE instance, where the
    # provider takes the `allowed_to_*` blocks (`push_access_level` is CE-only; found in a preview).
    out["main-protection"] = gitlab.BranchProtection(
        "main-protection", project=pid, branch="main", allowed_to_pushes=[{"access_level": "no one"}],
        allowed_to_merges=[{"access_level": "maintainer"}], allow_force_push=False,
        code_owner_approval_required=True, opts=after)
    out["push-rules"] = gitlab.ProjectPushRules("push-rules", project=pid, reject_unsigned_commits=True,
                                                prevent_secrets=True, opts=after)
    out["mr-approvals"] = gitlab.ProjectLevelMrApprovals("mr-approvals", project=pid,
                                                         merge_requests_author_approval=False, opts=after)
    out["any-approver"] = gitlab.ProjectApprovalRule("any-approver", project=pid, name="Any member",
                                                     rule_type="any_approver", approvals_required=1, opts=after)
    for label, color in LABELS.items():
        out[f"label-{label}"] = gitlab.ProjectLabel(f"label-{label.replace('::', '-')}", project=pid, name=label,
                                                    color=color, opts=after)
    out["cleanup-schedule"] = gitlab.PipelineSchedule("cleanup-schedule", project=pid, ref="refs/heads/main",
                                                      cron=CLEANUP_CRON, cron_timezone="UTC",
                                                      description="cleanup:previews", active=True, opts=after)
    return out
