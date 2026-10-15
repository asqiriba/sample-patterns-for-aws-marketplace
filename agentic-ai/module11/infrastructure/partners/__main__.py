# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-partners (stack `shared`): GitLab project governance and the Vercel project.

Run only through `python -m module11 partners up [--pipeline-only]`: it reads
devops-companion/partners/{gitlab-token,vercel-token} from Secrets Manager and passes them to this
program's process only (GITLAB_TOKEN, VERCEL_API_TOKEN). The program itself never reads a secret.

Config (non-secret): gitlabProject, gitlabProjectId, gitlabProjectName, botUserId, botLogin, botCommitEmail,
vercelTeamId, vercelTeamSlug,
approvers (JSON: {"harness-owners": [{"group_id": n} | {"user_id": n}], "platform": [...], "quality": [...]}),
importProject (set by the wrapper on the first run), platformStack.
CI variables come from the m11-platform stack's outputs (role ARNs, the OIDC audience) and the region.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pulumi  # noqa: E402
import pulumi_aws as aws  # noqa: E402

from module11.infrastructure.partners import gitlab_project, vercel_project  # noqa: E402
from module11.release import naming  # noqa: E402

config = pulumi.Config()
region = aws.get_region().region
platform = pulumi.StackReference(config.get("platformStack") or "organization/m11-platform/shared")
gitlab_roles = platform.get_output("gitlabRoles")
tags = {"Project": naming.PROJECT_TAG, "Stage": "shared"}

PENDING = "pending-m11-platform-pipeline"      # until m11-platform's enablePipeline=true creates the roles


def _role(name: str):
    return gitlab_roles.apply(lambda r: (r or {}).get(name) or PENDING)


variables = {
    "AWS_REGION": region,
    "M11_OIDC_AUD": platform.get_output("oidcAudience").apply(lambda v: v or "sts.amazonaws.com"),
    "M11_GITLAB_CI_ROLE_ARN": _role("m11-gitlab-ci"),
    "M11_GITLAB_CI_INFRA_ROLE_ARN": _role("m11-gitlab-ci-infra"),
    "M11_PROMOTER_ROLE_ARN": _role("m11-gitlab-promoter"),
}
settings = gitlab_project.Settings(
    project_id=config.require("gitlabProjectId"),
    project_name=config.get("gitlabProjectName") or "devops-companion-harness",
    bot_user_id=int(config.require("botUserId")),
    approvers=json.loads(config.require("approvers")),
    variables=variables,
    import_project=config.get_bool("importProject") or False,
)
resources = gitlab_project.deploy(settings)
ui = vercel_project.deploy(vercel_project.Settings(
    team_id=config.require("vercelTeamId"),
    gitlab_repository=config.require("gitlabProject"),
    bot_login=config.require("botLogin"), tags=tags))

pulumi.export("gitlabProjectId", settings.project_id)
pulumi.export("subClaimComponents", gitlab_project.SUB_CLAIM_COMPONENTS)
pulumi.export("protectedEnvironments", sorted(gitlab_project.ENVIRONMENTS))
pulumi.export("ciVariables", sorted(variables))
pulumi.export("botLogin", config.require("botLogin"))
pulumi.export("botCommitEmail", config.get("botCommitEmail") or f"{config.require('botLogin')}@noreply.gitlab.com")
pulumi.export("vercelTeamId", config.require("vercelTeamId"))
pulumi.export("vercelTeamSlug", config.require("vercelTeamSlug"))
