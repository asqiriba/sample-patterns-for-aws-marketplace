# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GitLab OIDC provider and the `m11-gitlab-ci`, `m11-gitlab-ci-infra` and `m11-gitlab-promoter`
roles.

The provider `https://gitlab.com` is created and tagged unless config `gitlabOidcProviderArn` names
an existing one, which is then used unmodified (its audience comes from `gitlabOidcAudience`, the
value published as the GitLab CI variable M11_OIDC_AUD, N12). Trusts check `gitlab.com:aud` and a
`StringLike gitlab.com:sub` built from the project's `ci_id_token_sub_claim_components`
`[project_path, ref_type, ref, environment_protected]` (release/oidc_sub.py renders the same form).
"""
from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

from module11.infrastructure.platform import pipeline_roles as PR


def provider(existing_arn: str | None, audience: str, tags: dict) -> pulumi.Output[str]:
    if existing_arn:
        return pulumi.Output.from_input(existing_arn)
    created = aws.iam.OpenIdConnectProvider("gitlab-oidc", url=f"https://{PR.GITLAB_ISSUER}", client_id_lists=[audience],
                                            tags={**tags, "M11Role": "oidc"})
    return created.arn


def roles(ctx_out: pulumi.Output, provider_arn: pulumi.Output[str], tags: dict) -> dict[str, aws.iam.Role]:
    """{name: role}; each trust and inline policy is rendered by pipeline_roles.gitlab_roles()."""
    out = {}
    for name in PR.OIDC_ROLES:
        rendered = pulumi.Output.all(ctx_out, provider_arn).apply(lambda v, n=name: PR.gitlab_roles(v[0])[n])
        role = aws.iam.Role(name, name=name, assume_role_policy=rendered.apply(lambda r: json.dumps(r[0])),
                            max_session_duration=3600, description=f"GitLab CI OIDC role {name} (design 4.5.1)",
                            tags={**tags, "M11Role": "gitlab-ci"})
        aws.iam.RolePolicy(f"{name}-policy", role=role.id, policy=rendered.apply(lambda r: json.dumps(r[1])))
        out[name] = role
    return out
