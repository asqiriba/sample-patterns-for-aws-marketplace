# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GitLab OIDC sub claims against the m11 trust patterns."""
from module11.release import oidc_sub as O

PROJECT = "example-group/devops-companion-harness"


def test_mr_job_matches_only_the_ci_role():
    job = {"project_path": PROJECT, "ref_type": "branch", "ref": "friday-runbook-tweak"}
    assert O.render(job) == f"project_path:{PROJECT}:ref_type:branch:ref:friday-runbook-tweak"
    assert O.roles_for(PROJECT, job) == ["m11-gitlab-ci"]
    unprotected = {**job, "environment_protected": False}
    assert O.render(unprotected).endswith(":environment_protected:false")
    assert O.roles_for(PROJECT, unprotected) == ["m11-gitlab-ci"]


def test_protected_environment_mr_job_matches_infra_but_never_the_promoter():
    job = {"project_path": PROJECT, "ref_type": "branch", "ref": "tooling-change", "environment_protected": True}
    assert O.roles_for(PROJECT, job) == ["m11-gitlab-ci", "m11-gitlab-ci-infra"]


def test_tag_job_matches_the_promoter_only_in_a_protected_environment():
    tag = {"project_path": PROJECT, "ref_type": "tag", "ref": "harness/v8", "environment_protected": True}
    assert O.roles_for(PROJECT, tag) == ["m11-gitlab-promoter"]
    assert O.roles_for(PROJECT, {**tag, "environment_protected": False}) == []
    assert O.roles_for(PROJECT, {**tag, "ref": "v8"}) == []
    assert O.roles_for(PROJECT, {**tag, "project_path": "fork/devops-companion-harness"}) == []


def test_branch_named_like_a_tag_never_matches_the_promoter():
    job = {"project_path": PROJECT, "ref_type": "branch", "ref": "harness/v9", "environment_protected": True}
    assert "m11-gitlab-promoter" not in O.roles_for(PROJECT, job)
