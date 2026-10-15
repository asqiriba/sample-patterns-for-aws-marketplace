# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GitLab OIDC `sub` claims and the m11 trust patterns. Pure.

The project customizes the `sub` claim to the components
`[project_path, ref_type, ref, environment_protected]`; `render()` builds the claim GitLab would
issue for a job, and `matches()` applies an IAM `StringLike` pattern to it. The tests show an MR
job never matches the promoter or `-infra` trust and a tag job in an unprotected environment
never matches the promoter (H3).
"""
from __future__ import annotations

from typing import Mapping

from .iam_cover import glob_match

COMPONENTS = ("project_path", "ref_type", "ref", "environment_protected")


def render(job: Mapping[str, object], components: tuple[str, ...] = COMPONENTS) -> str:
    """`job`: {project_path, ref_type (branch|tag), ref, environment_protected (bool or None)}.
    A job without an environment omits `environment_protected`, as GitLab does."""
    parts = []
    for name in components:
        value = job.get(name)
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{name}:{value}")
    return ":".join(parts)


def trust_patterns(project_path: str) -> dict[str, str]:
    """The `StringLike gitlab.com:sub` value of each m11 GitLab role."""
    base = f"project_path:{project_path}"
    return {
        "m11-gitlab-ci": f"{base}:ref_type:branch:ref:*",
        "m11-gitlab-ci-infra": f"{base}:ref_type:branch:ref:*:environment_protected:true",
        "m11-gitlab-promoter": f"{base}:ref_type:tag:ref:harness/v*:environment_protected:true",
    }


def matches(pattern: str, sub: str) -> bool:
    return glob_match(pattern, sub)


def roles_for(project_path: str, job: Mapping[str, object]) -> list[str]:
    sub = render(job)
    return sorted(role for role, pattern in trust_patterns(project_path).items() if matches(pattern, sub))
