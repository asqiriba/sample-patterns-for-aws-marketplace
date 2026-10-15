# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pure decision functions behind the CrossGuard pack P1-P11.

Importable without pulumi_policy: the pack's `__main__.py` wraps each function in a
ResourceValidationPolicy / StackValidationPolicy, and the tests call them directly with
constructed resource arguments. Every function returns a list of violation messages (empty
when compliant).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from module11.release import iam_cover
from module11.release.iam_cover import as_list, statements

PROJECT = "m11-devops-companion"
ACTIONS_FILE = Path(__file__).with_name("no_resource_actions.json")
SECRET_ENV = re.compile(r"(SECRET|TOKEN|PASSWORD|KEY)$")
ARN = re.compile(r"^arn:aws[a-z-]*:")
BOUNDARY_NAME = "m11-mr-boundary"
CI_DENY_KEYS = (
    "codebuild:source", "codebuild:source.buildspec", "codebuild:source.location", "codebuild:source.auth.type",
    "codebuild:source.insecureSsl", "codebuild:secondarySources", "codebuild:secondaryArtifacts",
    "codebuild:artifacts", "codebuild:cache", "codebuild:logsConfig", "codebuild:serviceRole",
    "codebuild:encryptionKey", "codebuild:autoRetryLimit", "codebuild:environment.image",
    "codebuild:environment.type", "codebuild:environment.computeType", "codebuild:environment.privilegedMode",
    "codebuild:environment.certificate", "codebuild:environment.registryCredential",
    "codebuild:environment.imagePullCredentialsType", "codebuild:environment.fleet.fleetArn",
)


@lru_cache(maxsize=1)
def action_lists() -> dict:
    """{noResource: [...], requiresCondition: {...}} from no_resource_actions.json."""
    return json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))


def _lower(values: Iterable[str]) -> set[str]:
    return {v.lower() for v in values}


# ---------------------------------------------------------------- P1a-P1d: IAM statements
def p1a(policy: Mapping | Sequence) -> list[str]:
    out = []
    for s in statements(policy):
        for action in as_list(s.get("Action")) + as_list(s.get("NotAction")):
            if "*" in action:
                out.append(f"P1a: wildcard action {action!r} (Sid {s.get('Sid')})")
    return out


def _metric_namespace_ok(s: Mapping) -> bool:
    cond = s.get("Condition") or {}
    for op in ("StringEquals", "StringLike"):
        values = as_list((cond.get(op) or {}).get("cloudwatch:namespace"))
        if values and all(v.startswith("m11/") or v == "bedrock-agentcore" for v in values):
            return True
    return False


def _create_guardrail_ok(s: Mapping, policy_tags: Mapping | None, role_name: str | None, stage: str | None) -> bool:
    if (policy_tags or {}).get("M11Role") != "deployer" or role_name == "m11-preview-infra":
        return False
    preview = role_name in iam_cover.PREVIEW_ROLES
    return iam_cover._request_tag_conditions(s, stage or "", preview)  # noqa: SLF001 - shared template check


def p1b(policy: Mapping | Sequence, *, policy_tags: Mapping | None = None, role_name: str | None = None,
        stage: str | None = None) -> list[str]:
    lists = action_lists()
    free = _lower(lists["noResource"])
    out = []
    for s in statements(policy):
        if s.get("Effect", "Allow") != "Allow" or "*" not in as_list(s.get("Resource")):
            continue
        for action in as_list(s.get("Action")):
            a = action.lower()
            if a in free:
                continue
            if a == "cloudwatch:putmetricdata" and _metric_namespace_ok(s):
                continue
            if a == "bedrock:createguardrail" and _create_guardrail_ok(s, policy_tags, role_name, stage):
                continue
            out.append(f'P1b: Resource "*" for {action} (Sid {s.get("Sid")})')
    return out


def p1c(policy: Mapping | Sequence, *, policy_tags: Mapping | None = None) -> list[str]:
    """Region wildcards only for bedrock:InvokeModel* on an exact base model; the boundary
    policy (M11Role=boundary) may name `foundation-model/*` (P1d position 5)."""
    boundary = (policy_tags or {}).get("M11Role") == "boundary"
    out = []
    for s in statements(policy):
        for resource in as_list(s.get("Resource")):
            parts = resource.split(":")
            if len(parts) > 3 and parts[3] == "*":
                model = resource.split("foundation-model/", 1)[-1]
                ok = (parts[2] == "bedrock" and resource.split(":", 5)[-1].startswith("foundation-model/")
                      and ("*" not in model or (boundary and model == "*"))
                      and all(a.lower().startswith("bedrock:invokemodel") for a in as_list(s.get("Action"))))
                if not ok:
                    out.append(f"P1c: region wildcard in {resource}")
    return out


def p1d(policy: Mapping | Sequence, *, policy_tags: Mapping | None, stagekey: str, stage: str | None = None,
        role_name: str | None = None) -> list[str]:
    stmts = statements(policy)
    out = []
    for s in stmts:
        if s.get("Effect", "Allow") != "Allow":
            continue
        for resource in as_list(s.get("Resource")):
            if resource == "*" or "*" not in resource:
                continue
            if resource.split(":")[3:4] == ["*"]:
                continue          # P1c's
            if not iam_cover.resource_wildcard_ok(resource, s, policy_tags, stagekey, stage=stage,
                                                  role_name=role_name, policy=stmts):
                out.append(f"P1d: wildcard not allowed in {resource} (Sid {s.get('Sid')})")
    return out


def p1(policy: Mapping | Sequence, *, policy_tags: Mapping | None, stagekey: str, stage: str | None = None,
       role_name: str | None = None) -> list[str]:
    return (p1a(policy) + p1b(policy, policy_tags=policy_tags, role_name=role_name, stage=stage or stagekey)
            + p1c(policy, policy_tags=policy_tags)
            + p1d(policy, policy_tags=policy_tags, stagekey=stagekey, stage=stage, role_name=role_name))


# ---------------------------------------------------------------- P2-P9: resource arguments
def p2(function_args: Mapping[str, Any]) -> list[str]:
    return [] if function_args.get("publish") is True else [f"P2: {function_args.get('name')} must publish=True"]


def p3(resource_name: str, tags: Mapping[str, str] | None) -> list[str]:
    tags = tags or {}
    out = []
    if tags.get("Project") != PROJECT:
        out.append(f"P3: {resource_name} lacks Project={PROJECT}")
    if not tags.get("Stage"):
        out.append(f"P3: {resource_name} lacks a Stage tag")
    return out


def p4(env: Mapping[str, str] | None) -> list[str]:
    return [f"P4: env {k} must hold an ARN, not a value" for k, v in sorted((env or {}).items())
            if SECRET_ENV.search(k) and not ARN.match(str(v))]


def p5(log_group_name: str, retention_days: int | None) -> list[str]:
    if retention_days is None or not 0 < int(retention_days) <= 30:
        return [f"P5: {log_group_name} retention must be set to 30 days or less"]
    return []


SERVICE_PRINCIPALS = ("lambda.amazonaws.com", "bedrock-agentcore.amazonaws.com")


def p6(role_name: str, trust: Mapping | Sequence) -> list[str]:
    out = []
    for s in statements(trust):
        services = as_list((s.get("Principal") or {}).get("Service")) if isinstance(s.get("Principal"), Mapping) else []
        if not set(services) & set(SERVICE_PRINCIPALS):
            continue
        cond = s.get("Condition") or {}
        if not any("aws:SourceAccount" in (cond.get(op) or {}) for op in ("StringEquals", "StringLike")):
            out.append(f"P6: {role_name} trust for {services} lacks aws:SourceAccount")
    return out


def p7(stage: str, network_mode: str, allow_public_network: bool) -> list[str]:
    if stage == "prod" and network_mode == "PUBLIC" and not allow_public_network:
        return ["P7: a prod runtime is PUBLIC without allowPublicNetwork=true"]
    return []


def p8(stage: str, role_name: str, permissions_boundary: str | None) -> list[str]:
    if not stage.startswith("mr-"):
        return []
    if not permissions_boundary or not permissions_boundary.endswith(f":policy/{BOUNDARY_NAME}"):
        return [f"P8: {role_name} in {stage} must carry permissions_boundary {BOUNDARY_NAME}"]
    return []


_QUALIFIED = re.compile(r"^arn:aws:lambda:[a-z0-9-]+:\d{12}:function:m11-[a-z0-9_-]+:\d+$")


def p9(invoke_resources: Sequence[str | None], retained_tool_arns: Iterable[str], stack_tool_outputs: Iterable[str],
       pending_version: bool) -> list[str]:
    """The runtime role's lambda:InvokeFunction resources (None = unknown at preview)."""
    allowed = set(retained_tool_arns) | set(stack_tool_outputs)
    out = []
    for arn in invoke_resources:
        if arn is None:
            if not pending_version:
                out.append("P9: unknown invoke resource with no pending tool version")
            continue
        if not _QUALIFIED.match(arn):
            out.append(f"P9: {arn} is not a qualified tool ARN")
        elif arn not in allowed:
            out.append(f"P9: {arn} is neither a retained pin nor this stack's tool version")
    return out


# ---------------------------------------------------------------- P10, P11: m11-platform
NO_CACHE_PROJECTS = ("m11-preview-image", "m11-preview-infra")


def p10(projects: Sequence[Mapping[str, Any]], artifacts_bucket: str,
        role_policies: Mapping[str, Mapping | Sequence] | None = None) -> list[str]:
    """projects: [{name, cache: {type, location?}}]; role_policies: {project name: its role policy}."""
    out, seen = [], {}
    for project in projects:
        name, cache = project["name"], project.get("cache") or {"type": "NO_CACHE"}
        kind = cache.get("type", "NO_CACHE")
        if name in NO_CACHE_PROJECTS and kind != "NO_CACHE":
            out.append(f"P10: {name} must use NO_CACHE")
            continue
        if kind == "NO_CACHE":
            continue
        want = f"{artifacts_bucket}/cache/{name}"
        if kind != "S3" or cache.get("location") != want:
            out.append(f"P10: {name} cache must be S3 at {want}")
        location = cache.get("location")
        if location in seen:
            out.append(f"P10: {name} shares its cache location with {seen[location]}")
        seen[location] = name
    for name, policy in (role_policies or {}).items():
        own = f"arn:aws:s3:::{artifacts_bucket}/cache/{name}/*"
        for s in statements(policy):
            if s.get("Effect", "Allow") != "Allow":
                continue
            writes = [a for a in as_list(s.get("Action")) if a.lower() in ("s3:putobject", "s3:deleteobject")]
            for resource in as_list(s.get("Resource")) if writes else []:
                if "/cache/" in resource and resource != own:
                    out.append(f"P10: {name}'s role may write {resource}; only {own}")
    return out


def is_ci_start_policy(policy: Mapping | Sequence, project_arn: str, names: Sequence[str]) -> bool:
    """The ci_start_policy shape: one Allow with the ForAllValues name list and the 21
    one-key Null Denies."""
    stmts = statements(policy)
    allows = [s for s in stmts if s.get("Effect", "Allow") == "Allow"
              and "codebuild:startbuild" in _lower(as_list(s.get("Action")))]
    if len(allows) != 1 or as_list(allows[0].get("Resource")) != [project_arn]:
        return False
    listed = ((allows[0].get("Condition") or {}).get("ForAllValues:StringEquals") or {}).get(
        "codebuild:environment.environmentVariables.name")
    if sorted(as_list(listed)) != sorted(names):
        return False
    denied = set()
    for s in stmts:
        if s.get("Effect") != "Deny":
            continue
        null = (s.get("Condition") or {}).get("Null") or {}
        if len(null) != 1 or len(s.get("Condition")) != 1:
            return False
        denied |= set(null)
    return denied == set(CI_DENY_KEYS)


def p11(policies: Mapping[str, Mapping | Sequence], seal_experiment_arn: str, pipeline_role: str,
        pipeline_projects: Sequence[str], source_types: Mapping[str, str]) -> list[str]:
    """policies: {policy or role name: document} on m11-platform."""
    out = []
    granting = [name for name, doc in policies.items()
                if any(iam_cover.grants(s, "codebuild:StartBuild", seal_experiment_arn) for s in statements(doc))]
    others = sorted(set(granting) - {"m11-operator-experiment"})
    if others:
        out.append(f"P11: StartBuild on m11-seal-experiment granted by {', '.join(others)}")
    if "m11-operator-experiment" in policies and "m11-operator-experiment" not in granting:
        out.append("P11: m11-operator-experiment does not grant StartBuild on m11-seal-experiment")
    if "m11-operator-experiment" in policies and not is_ci_start_policy(
            policies["m11-operator-experiment"], seal_experiment_arn, ["SOURCE_SHA", "EXPERIMENT_ID"]):
        out.append("P11: m11-operator-experiment's StartBuild statement is not the ci_start_policy shape")
    for s in statements(policies.get(pipeline_role, [])):
        if "codebuild:startbuild" in _lower(as_list(s.get("Action"))):
            for resource in as_list(s.get("Resource")):
                if "*" in resource or resource not in pipeline_projects:
                    out.append(f"P11: pipeline role StartBuild on {resource}; only its own action projects")
    for name, want in (("m11-seal", "CODEPIPELINE"), ("m11-seal-experiment", "NO_SOURCE")):
        if name in source_types and source_types[name] != want:
            out.append(f"P11: {name} source type must be {want}")
    return out


# ---------------------------------------------------------------- operator trust
def operator_trust(role_name: str, trust: Mapping | Sequence, operator_principals: Sequence[str]) -> list[str]:
    ops = set(operator_principals)
    if not ops:
        return []
    found_assume = found_source = False
    out = []
    for s in statements(trust):
        principal = s.get("Principal") or {}
        aws = set(as_list(principal.get("AWS"))) if isinstance(principal, Mapping) else set()
        if not aws & ops:
            continue
        actions = _lower(as_list(s.get("Action")))
        cond = s.get("Condition") or {}
        if s.get("Sid") == "OperatorAssume" and actions == {"sts:assumerole"} and \
                str((cond.get("StringLike") or {}).get("sts:RoleSessionName")) == "m11-operator-*" and \
                str((cond.get("Null") or {}).get("sts:SourceIdentity")) == "false":
            found_assume = True
        elif s.get("Sid") == "OperatorSourceIdentity" and actions == {"sts:setsourceidentity"}:
            found_source = True
        else:
            out.append(f"operator trust: {role_name} has another statement for operator principals (Sid {s.get('Sid')})")
    if not found_assume:
        out.append(f"operator trust: {role_name} lacks OperatorAssume")
    if not found_source:
        out.append(f"operator trust: {role_name} lacks OperatorSourceIdentity")
    return out
