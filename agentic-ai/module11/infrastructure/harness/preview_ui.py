# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Vercel environment variables: branch-scoped preview variables for an
`mr-*` stack when `release deploy`'s UI decision is `wire`, production variables for prod.

Enabled per stack by config `enableVercel` (with `vercelProjectId`, `vercelTeamId` and, for mr-*, `gitBranch`),
which `release deploy` sets only when the UI decision is `wire`. The Vercel provider reads its token
from VERCEL_API_TOKEN, which only that deploy passes; every other deploy declares none of these resources, and
`--exclude-ui` leaves them untouched. Values are not secrets: the stage's API URL, its browser client
ID and Module 10's hosted-login URL, the same fields `module10/deployment/pulumi/ui.py` writes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                       # the URN helpers run where Pulumi is not installed (the MR validate image)
    from .base import Stage

KEYS = ("M11_API_URL", "M11_CLIENT_ID", "M11_AUTH_URL")
URN_TYPE = "vercel:index/projectEnvironmentVariable:ProjectEnvironmentVariable"


def logical_name(key: str) -> str:
    return f"ui-{key.lower().replace('_', '-')}"


def urns(stack: str) -> list[str]:
    """The URNs `--exclude-ui` passes to `pulumi up --exclude`."""
    return [f"urn:pulumi:{stack}::m11-harness::{URN_TYPE}::{logical_name(k)}" for k in KEYS]


def declare(stage: "Stage", *, enabled: bool, api_url: Any = None, client_id: Any = None, auth_url: Any = None,
            project_id: str | None = None, team_id: str | None = None, git_branch: str | None = None) -> dict:
    if not enabled:
        return {}
    import pulumi
    import pulumiverse_vercel as vercel
    if not project_id or not team_id:
        raise ValueError("enableVercel needs vercelProjectId and vercelTeamId")
    is_mr = stage.settings.is_mr
    if is_mr and not git_branch:
        raise ValueError("an mr-* stack's Vercel variables need gitBranch (the MR's source branch)")
    values = {"M11_API_URL": api_url, "M11_CLIENT_ID": client_id, "M11_AUTH_URL": auth_url}
    out = {}
    for key in KEYS:
        out[key] = vercel.ProjectEnvironmentVariable(
            logical_name(key), project_id=project_id, team_id=team_id, key=key,
            value=pulumi.Output.from_input(values[key]).apply(lambda v: str(v).rstrip("/") if v else ""),
            targets=["preview"] if is_mr else ["production"], git_branch=git_branch if is_mr else None,
            sensitive=False, comment=f"m11 {stage.name} (release deploy)")
    return out
