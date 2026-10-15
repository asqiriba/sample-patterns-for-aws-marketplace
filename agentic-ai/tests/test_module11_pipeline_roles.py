# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-platform's pipeline IAM and the pack's P10/P11 wiring."""
from __future__ import annotations

import json

import pytest

from module11.infrastructure.platform import pipeline_roles as PR
from module11.infrastructure.platform import roles as R
from module11.infrastructure.policy import pack, rules
from module11.release import iam_cover, oidc_sub

A = "111122223333"
CTX = R.Ctx(account=A, region="us-east-1", state_key_arn=f"arn:aws:kms:us-east-1:{A}:key/a",
            preview_key_arn=f"arn:aws:kms:us-east-1:{A}:key/b", user_pool_id="us-east-1_X", state_table="m10-state")
C = PR.PipelineCtx(CTX, f"arn:aws:codeconnections:us-east-1:{A}:connection/abc", "example-group/devops-companion-harness")
TOKEN = f"arn:aws:secretsmanager:us-east-1:{A}:secret:devops-companion/partners/gitlab-token-abcdef"


def all_policies() -> dict[str, dict]:
    docs = {PR.PIPELINE_ROLE: PR.pipeline_policy(C), "m11-operator-experiment": PR.operator_experiment_policy(C),
            "m11-freeze-check": PR.freeze_check_policy(C), "m11-gitlab-notes": PR.notes_policy(C)}
    docs.update({name: fn(C) for name, fn in PR.PROJECT_POLICIES.items()})
    docs.update({name: pol for name, (_, pol) in PR.gitlab_roles(C).items()})
    return docs


@pytest.mark.parametrize("name", sorted(all_policies()))
def test_every_phase6_policy_passes_p1_and_fits(name):
    doc = all_policies()[name]
    assert pack.p1_any(doc, policy_tags=pack.role_kind(name), stage=pack.stage_of(name), role_name=name) == []
    assert R.chars(doc) <= R.INLINE_LIMIT


def test_preview_infra_managed_policies_have_no_creates_and_no_cognito_writes():
    docs = R.preview_infra_policies(CTX)
    statements = [s for d in docs.values() for s in rules.statements(d)]
    assert not {s["Sid"] for s in statements} & {"CreateApi", "CreateGuardrail", "CreateTagGuardrail"}
    actions = {a for s in statements if s["Effect"] == "Allow" for a in rules.as_list(s["Action"])}
    assert "bedrock:CreateGuardrail" not in actions
    assert not {a for a in actions if a.startswith("cognito-idp:") and not a.startswith("cognito-idp:Describe")}
    collections = {CTX.apigw(k) for k in ("restapis", "usageplans", "apikeys")}
    assert not any(set(rules.as_list(s["Resource"])) & collections for s in statements if s["Effect"] == "Allow")
    for name, doc in docs.items():
        role = pack._deployer_role_of_policy(name)
        assert role == "m11-preview-infra"
        assert pack.p1_any(doc, policy_tags={"M11Role": "deployer"}, stage="mr", role_name=role) == []
        assert R.chars(doc) <= R.MANAGED_POLICY_LIMIT


def _grants(doc, action, resource):
    return any(iam_cover.grants(s, action, resource) for s in rules.statements(doc))


def test_only_the_named_roles_read_the_bot_token():
    readers = {n for n, d in all_policies().items() if _grants(d, "secretsmanager:GetSecretValue", TOKEN)}
    assert readers == {"m11-seal", "m11-bot", "m11-gitlab-notes"}
    for name in PR.denied_token_readers():
        assert name not in readers


def test_previews_invoke_the_broker_instead():
    broker = f"arn:aws:lambda:us-east-1:{A}:function:{PR.NOTES_FUNCTION}"
    for name in PR.broker_callers():
        assert _grants(all_policies()[name], "lambda:InvokeFunction", broker), name


def test_p11_only_the_experiment_operator_starts_seal_experiment():
    seal_exp = C.project_arn("m11-seal-experiment")
    pipeline_projects = [C.project_arn(p) for p in PR.ACTION_PROJECTS]
    docs = all_policies()
    assert rules.p11(docs, seal_exp, PR.PIPELINE_ROLE, pipeline_projects,
                     {"m11-seal": "CODEPIPELINE", "m11-seal-experiment": "NO_SOURCE"}) == []
    assert rules.is_ci_start_policy(docs["m11-operator-experiment"], seal_exp, list(PR.EXPERIMENT_NAMES))
    bad = dict(docs, **{"m11-bake": R.doc([R.allow("X", "codebuild:StartBuild", C.project_arn("m11-*"))])})
    assert any("granted by m11-bake" in m for m in rules.p11(bad, seal_exp, PR.PIPELINE_ROLE, pipeline_projects, {}))
    assert rules.p11(docs, seal_exp, PR.PIPELINE_ROLE, pipeline_projects, {"m11-seal": "NO_SOURCE"})


def test_pipeline_role_starts_only_its_own_action_projects():
    starts = [s for s in rules.statements(PR.pipeline_policy(C)) if "codebuild:StartBuild" in rules.as_list(s["Action"])]
    assert starts and all(set(rules.as_list(s["Resource"])) == {C.project_arn(p) for p in PR.ACTION_PROJECTS}
                          for s in starts)


def test_caches_are_per_project_and_untrusted_projects_have_none():
    for name in PR.PROJECTS:
        doc = all_policies().get(name) or R.doc(PR.image_policy_extra(C))
        caches = [r for s in rules.statements(doc) for r in rules.as_list(s.get("Resource")) if "/cache/" in r]
        if name in PR.UNTRUSTED_PROJECTS:
            assert caches == [], name
        else:
            assert caches == [f"arn:aws:s3:::{CTX.pipeline_bucket}/cache/{name}/*"], name
    projects = [{"name": n, "cache": {"type": "S3", "location": f"{CTX.pipeline_bucket}/cache/{n}"}}
                for n in PR.TRUSTED_PROJECTS] + [{"name": n, "cache": {"type": "NO_CACHE"}} for n in PR.UNTRUSTED_PROJECTS]
    roles_ = {n: all_policies()[n] for n in PR.PROJECT_POLICIES}
    assert rules.p10(projects, CTX.pipeline_bucket, roles_) == []


def test_gitlab_trusts_use_the_documented_sub_claims():
    roles_ = PR.gitlab_roles(C)
    subs = {n: t["Statement"][0]["Condition"]["StringLike"]["gitlab.com:sub"] for n, (t, _) in roles_.items()}
    assert subs["m11-gitlab-promoter"] == ("project_path:example-group/devops-companion-harness:ref_type:tag:"
                                           "ref:harness/v*:environment_protected:true")
    assert subs["m11-gitlab-ci"].endswith(":ref_type:branch:ref:*")
    assert subs["m11-gitlab-ci-infra"].endswith(":ref_type:branch:ref:*:environment_protected:true")
    for trust, _ in roles_.values():
        cond = trust["Statement"][0]["Condition"]
        assert cond["StringEquals"] == {"gitlab.com:aud": "sts.amazonaws.com"}
        assert trust["Statement"][0]["Principal"] == {"Federated": f"arn:aws:iam::{A}:oidc-provider/gitlab.com"}
    assert subs == oidc_sub.trust_patterns("example-group/devops-companion-harness")
    tag_job = {"project_path": "example-group/devops-companion-harness", "ref_type": "tag", "ref": "harness/v3",
               "environment_protected": True}
    assert oidc_sub.roles_for("example-group/devops-companion-harness", tag_job) == ["m11-gitlab-promoter"]


def test_ci_roles_use_the_start_policy_with_their_names():
    roles_ = PR.gitlab_roles(C)
    assert rules.is_ci_start_policy(roles_["m11-gitlab-ci"][1], C.project_arn("m11-preview"), list(PR.PREVIEW_NAMES))
    assert rules.is_ci_start_policy(roles_["m11-gitlab-ci-infra"][1], C.project_arn("m11-preview-infra"),
                                    list(PR.PREVIEW_INFRA_NAMES))
    promoter = roles_["m11-gitlab-promoter"][1]
    approve = [s for s in promoter["Statement"] if s["Action"] == "codepipeline:PutApprovalResult"]
    assert approve and set(approve[0]["Resource"]) == {f"{C.pipeline_arn}/Approval/ReleaseApproval",
                                                       f"{C.pipeline_arn}/ExtendedApproval/ExtendedReleaseApproval"}


def test_pack_wires_p10_and_p11_from_stack_resources():
    bucket = CTX.pipeline_bucket
    resources = [("aws:s3/bucket:Bucket", {"bucket": bucket})]
    for name in PR.PROJECTS:
        cache = {"type": "NO_CACHE"} if name in PR.UNTRUSTED_PROJECTS else {"type": "S3", "location": f"{bucket}/cache/{name}"}
        resources.append((pack.CODEBUILD, {"name": name, "arn": C.project_arn(name), "cache": cache,
                                           "serviceRole": f"arn:aws:iam::{A}:role/{PR.PROJECT_ROLES[name]}",
                                           "source": {"type": "CODEPIPELINE" if name in PR.SOURCE_PROJECTS else "NO_SOURCE"}}))
    for name, doc in all_policies().items():
        resources.append((pack.ROLE_POLICY, {"role": name, "policy": json.dumps(doc)}))
    assert pack.check_stack(resources) == []
    shared = [(t, dict(p, cache={"type": "S3", "location": f"{bucket}/cache/m11-deploy"}) if p.get("name") == "m11-gate"
               else p) for t, p in resources]
    assert any("P10" in m for m in pack.check_stack(shared))
    leaky = resources + [(pack.ROLE_POLICY, {"role": "m11-bake", "policy": json.dumps(R.doc([
        R.allow("X", "codebuild:StartBuild", C.project_arn("m11-seal-experiment"))]))})]
    assert any("P11" in m for m in pack.check_stack(leaky))
