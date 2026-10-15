# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Vercel project for per-MR previews of the Module 10 UI (pulumiverse-vercel 5.4.1).

- Project `devops-companion-ui` in the team from config, rooted at the repository root (Vercel's Root
  Directory cannot read outside it, and the build copies module10/ui/*), linked to the GitLab project.
- Build `node module11/vercel/build.mjs`, output `module11/vercel/dist`, ignored-build step
  `node module11/vercel/ignore.mjs` (bot commits and unwired branches never build).
- Standard Protection on previews; a generated protection-bypass secret for the gate's automated checks,
  stored in Secrets Manager `devops-companion/partners/vercel-bypass` (never in a log or a variable).
- The non-secret project variable `M11_BOT_LOGIN` that ignore.mjs reads.
Branch-scoped preview variables are written per MR by `release deploy`, not here.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws
import pulumiverse_vercel as vercel

PROJECT_NAME = "devops-companion-ui"
BYPASS_SECRET = "devops-companion/partners/vercel-bypass"  # nosec B105 - a Secrets Manager secret name, not a secret value


@dataclass(frozen=True)
class Settings:
    team_id: str
    gitlab_repository: str          # <your-group>/devops-companion-harness
    bot_login: str
    tags: dict


def deploy(settings: Settings) -> dict:
    project = vercel.Project(
        "ui-project", name=PROJECT_NAME, team_id=settings.team_id, framework=None, root_directory=None,
        build_command="node module11/vercel/build.mjs", output_directory="module11/vercel/dist",
        install_command="true", ignore_command="node module11/vercel/ignore.mjs", node_version="24.x",
        # build.mjs reads VERCEL_BRANCH_URL and ignore.mjs VERCEL_GIT_COMMIT_AUTHOR_LOGIN; without
        # this the first wired MR's build failed with "VERCEL_BRANCH_URL is required".
        automatically_expose_system_environment_variables=True,
        git_repository={"type": "gitlab", "repo": settings.gitlab_repository},
        vercel_authentication={"deployment_type": "standard_protection"})
    bypass = vercel.ProjectProtectionBypass("ui-bypass", project_id=project.id, team_id=settings.team_id,
                                            note="m11 gate automation (design 4.2.13)")
    secret = aws.secretsmanager.Secret("vercel-bypass-secret", name=BYPASS_SECRET, recovery_window_in_days=0,
                                       description="Vercel protection-bypass secret for devops-companion-ui previews",
                                       tags=settings.tags)
    aws.secretsmanager.SecretVersion("vercel-bypass-value", secret_id=secret.id,
                                     secret_string=pulumi.Output.secret(bypass.secret))
    vercel.ProjectEnvironmentVariable("ui-bot-login", project_id=project.id, team_id=settings.team_id,
                                      key="M11_BOT_LOGIN", value=settings.bot_login,
                                      targets=["preview", "production"], sensitive=False)
    pulumi.export("vercelProjectId", project.id)
    return {"project": project, "bypass": bypass, "bypassSecret": secret}
