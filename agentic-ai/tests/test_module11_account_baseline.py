# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`m11-account-baseline` under Pulumi mocks (docs/MULTI_ACCOUNT.md): one stage account's deployer role
from the same templates as the single-account build: the same five policy parts, P1-clean, the tooling account's
pipeline roles plus the operators in the trust, and the project tags. Not deployed by this build."""
from __future__ import annotations

import json

import pytest

pulumi = pytest.importorskip("pulumi")
pytest.importorskip("pulumi_aws")

from module11.infrastructure.platform import roles as R  # noqa: E402
from module11.infrastructure.policy import rules as P  # noqa: E402

STAGE_ACCOUNT, TOOLING = "444455556666", "111122223333"
OPERATOR = f"arn:aws:iam::{TOOLING}:user/operator"
CTX = R.Ctx(STAGE_ACCOUNT, "us-east-1", f"arn:aws:kms:us-east-1:{STAGE_ACCOUNT}:key/main",
            f"arn:aws:kms:us-east-1:{STAGE_ACCOUNT}:key/preview", "us-east-1_EXAMPLE", "request-state-abc")


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources = []

    def new_resource(self, args):
        self.resources.append(args)
        outputs = dict(args.inputs)
        if args.typ == "aws:iam/role:Role":
            outputs["arn"] = f"arn:aws:iam::{STAGE_ACCOUNT}:role/{args.inputs['name']}"
        if args.typ == "aws:iam/policy:Policy":
            outputs["arn"] = f"arn:aws:iam::{STAGE_ACCOUNT}:policy/{args.inputs['name']}"
        return f"{args.name}-id", outputs

    def call(self, args):
        return {}


@pytest.mark.parametrize("stage, callers", [("prod", ["m11-deploy", "m11-bake"]), ("staging", ["m11-deploy"]),
                                            ("dev", [])])
def test_account_baseline_deployer_role(stage, callers):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="m11-account-baseline", stack=stage, preview=False)
    from module11.infrastructure.platform import account_baseline as AB

    @pulumi.runtime.test
    def check():
        role = AB.deployer(stage, CTX, TOOLING, [OPERATOR])
        return role.arn.apply(lambda arn: arn)
    arn = check()
    del arn
    roles = [r for r in mocks.resources if r.typ == "aws:iam/role:Role"]
    policies = [r for r in mocks.resources if r.typ == "aws:iam/policy:Policy"]
    attachments = [r for r in mocks.resources if r.typ == "aws:iam/rolePolicyAttachment:RolePolicyAttachment"]
    assert [r.inputs["name"] for r in roles] == [f"m11-{stage}-deployer"]
    assert sorted(p.inputs["name"] for p in policies) == sorted(f"m11-{stage}-deployer-{x}" for x in R.OPS_PARTS)
    assert len(attachments) == len(R.OPS_PARTS)
    trust = json.loads(roles[0].inputs["assumeRolePolicy"])
    principals = [s["Principal"]["AWS"] for s in trust["Statement"]]
    assert [OPERATOR] in principals
    trusted = next((s["Principal"]["AWS"] for s in trust["Statement"] if s.get("Sid") == "TrustedRoles"), [])
    assert trusted == [f"arn:aws:iam::{TOOLING}:role/{c}" for c in callers]
    assert roles[0].inputs["tags"] == {"Project": "m11-devops-companion", "Stage": stage, "M11Role": "deployer"}
    for p in policies:
        doc = json.loads(p.inputs["policy"])
        assert P.p1(doc, policy_tags={"M11Role": "deployer"}, stagekey=stage, role_name=f"m11-{stage}-deployer") == []
        assert R.chars(doc) <= R.MANAGED_POLICY_LIMIT
        assert TOOLING not in json.dumps(doc)               # the stage account's resources only
