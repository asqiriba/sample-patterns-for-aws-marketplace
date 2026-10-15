# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`m11-account-baseline`: the per-account half of the multi-account layout (docs/MULTI_ACCOUNT.md).

In the recommended layout each stage has its own AWS account, and the deployment role is the only way in. This
program creates that role in one stage account from the same templates as the single-account build: the policies
are `roles.deployer_policies(ctx, stage)`, split into the same managed-policy parts, and the trust admits the
tooling account's pipeline roles (m11-deploy, plus m11-bake for prod) and the operator principals, with the same
session-name rule as `roles.role_trust`. Nothing here is deployed by this build (the demo runs in one account);
customers apply it once per stage account, from a Pulumi project named `m11-account-baseline` whose
`__main__.py` is `from module11.infrastructure.platform import account_baseline; account_baseline.run()`:

    pulumi stack init prod                           # one stack per stage account, with that account's credentials
    pulumi config set m11-account-baseline:stage prod
    pulumi config set m11-account-baseline:toolingAccountId <tooling account>
    pulumi config set --path 'm11-account-baseline:operatorPrincipals[0]' <operator ARN>
    pulumi up
"""
from __future__ import annotations

import json
from typing import Sequence

import pulumi
import pulumi_aws as aws

from module11.infrastructure.platform import roles as R
from module11.release import naming

CALLERS = {"dev": (), "staging": ("m11-deploy",), "prod": ("m11-deploy", "m11-bake")}


def trust(stage: str, tooling_account: str, operator_principals: Sequence[str], partition: str = "aws") -> dict:
    """The stage account's deployer trust: the tooling account's pipeline roles plus the operators."""
    roles = [f"arn:{partition}:iam::{tooling_account}:role/{name}" for name in CALLERS[stage]]
    return R.role_trust(list(operator_principals), roles)


def deployer(stage: str, ctx: R.Ctx, tooling_account: str, operator_principals: Sequence[str]) -> aws.iam.Role:
    name = naming.deployer_role(stage)
    tags = {"Project": naming.PROJECT_TAG, "Stage": stage, "M11Role": "deployer"}
    role = aws.iam.Role(name, name=name, assume_role_policy=json.dumps(trust(stage, tooling_account,
                                                                             operator_principals, ctx.partition)),
                        max_session_duration=R.DEPLOYER_SESSION_SECONDS,
                        description=f"The only path into the {stage} account: release deploy / prune (MULTI_ACCOUNT.md)",
                        tags=tags)
    docs = R.deployer_policies(ctx, stage)
    for part in R.OPS_PARTS:
        policy = aws.iam.Policy(f"{name}-{part}", name=f"{name}-{part}", policy=json.dumps(docs[f"{name}-{part}"]),
                                tags=tags)
        aws.iam.RolePolicyAttachment(f"{name}-{part}", role=role.name, policy_arn=policy.arn)
    return role


def run() -> None:
    config = pulumi.Config("m11-account-baseline")
    stage = naming.check_stage(config.require("stage"))
    identity = aws.get_caller_identity()
    region = aws.get_region().name
    ctx = R.Ctx(identity.account_id, region, config.get("stateKeyArn") or "", config.get("previewKeyArn") or "",
                config.get("userPoolId") or "", config.get("stateTable") or "")
    role = deployer(stage, ctx, config.require("toolingAccountId"), config.require_object("operatorPrincipals"))
    pulumi.export("deployerRole", role.arn)
    pulumi.export("stage", stage)
