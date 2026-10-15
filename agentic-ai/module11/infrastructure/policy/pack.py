# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Adapts Pulumi resource properties (camelCase, as CrossGuard sees them) to the pure rules in
rules.py. Pure: no pulumi_policy import, so tests call it with plain dicts.

`check_resource(type, props)` returns the violations of one resource (P1-P8). `check_stack(resources,
retained)` returns the stack-level ones (P9, P10, P11). A property that is still unknown at preview
(any non-string where a JSON document is expected) is skipped there and checked when `pulumi up`
registers the resource with resolved inputs.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Mapping, Sequence

from module11.release import naming

from . import rules

ROLE = "aws:iam/role:Role"
ROLE_POLICY = "aws:iam/rolePolicy:RolePolicy"
POLICY = "aws:iam/policy:Policy"
FUNCTION = "aws:lambda/function:Function"
LOG_GROUP = "aws:cloudwatch/logGroup:LogGroup"
RUNTIME = "aws:bedrock/agentcoreAgentRuntime:AgentcoreAgentRuntime"
CODEBUILD = "aws:codebuild/project:Project"
BUCKET = "aws:s3/bucket:Bucket"
NATIVE_PREFIX = "aws-native:"
# Types that take a `tags` map but whose tags are not checked here (provider or child resources).
UNTAGGED_OK = {"pulumi:providers:aws", "pulumi:providers:aws-native", "pulumi:pulumi:Stack"}
_STAGE = re.compile(r"^m11-(dev|staging|prod|mr-\d{1,9}|preview|image|shared)(-|$)")
_QUALIFIED_OWN = re.compile(r"^arn:aws:lambda:[a-z0-9-]+:\d{12}:function:m11-(dev|staging|prod|mr-\d+)-[a-z0-9_]+:\d+$")


def _json(value: Any) -> dict | list | None:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return None


def role_kind(role_name: str | None, tags: Mapping[str, str] | None = None) -> dict:
    """policy_tags for P1: the role's M11Role tag, or what its name says."""
    if tags and tags.get("M11Role"):
        return {"M11Role": tags["M11Role"]}
    name = role_name or ""
    if name.endswith("-deployer") or name in rules.iam_cover.PREVIEW_ROLES:
        return {"M11Role": "deployer"}
    if name.endswith("-controller"):
        return {"M11Role": "controller"}
    if name.endswith("-drift-check"):
        return {"M11Role": "drift-checker"}
    return {}


def stage_of(name: str | None, tags: Mapping[str, str] | None = None) -> str:
    if tags and tags.get("Stage"):
        return tags["Stage"]
    match = _STAGE.match(name or "")
    if not match:
        return "shared"
    return {"preview": "mr", "image": "shared"}.get(match.group(1), match.group(1))


def p1_any(policy: Mapping | Sequence, *, policy_tags: Mapping, stage: str, role_name: str | None) -> list[str]:
    """P1 with the stage name and the stage key (mr-0 / mr0): a violation must fail both."""
    keys = [stage] + ([naming.stagekey(stage)] if naming.MR_STAGE.match(stage) else [])
    results = [set(rules.p1(policy, policy_tags=policy_tags, stagekey=k, stage=stage if stage != "mr" else "mr",
                            role_name=role_name)) for k in keys]
    return sorted(set.intersection(*results)) if results else []


def _deployer_role_of_policy(name: str) -> str:
    """`m11-preview-deployer-edge` -> `m11-preview-deployer`, `m11-preview-infra-edge` -> `m11-preview-infra`
    (managed policies are split by area)."""
    if re.search(r"-deployer-[a-z]+$", name) or re.match(r"^m11-preview-infra-[a-z]+$", name):
        return name.rsplit("-", 1)[0]
    return name


def check_resource(resource_type: str, props: Mapping[str, Any], *, operators: Sequence[str] = ()) -> list[str]:
    tags = props.get("tags") if isinstance(props.get("tags"), Mapping) else None
    out: list[str] = []
    name = props.get("name") if isinstance(props.get("name"), str) else None
    if resource_type == ROLE:
        trust = _json(props.get("assumeRolePolicy"))
        if trust is not None:
            out += rules.p6(name or "?", trust)
            # The template applies to every trust that admits an operator principal.
            principals = {p for s in rules.statements(trust) if isinstance(s.get("Principal"), Mapping)
                          for p in rules.as_list(s["Principal"].get("AWS"))}
            named = {p for s in rules.statements(trust) if str(s.get("Sid", "")).startswith("Operator")
                     and isinstance(s.get("Principal"), Mapping) for p in rules.as_list(s["Principal"].get("AWS"))}
            ops = sorted((set(operators) & principals) | named)
            out += rules.operator_trust(name or "?", trust, ops)
        stage = stage_of(name, tags)
        boundary = props.get("permissionsBoundary")
        if naming.MR_STAGE.match(stage) and (boundary is None or isinstance(boundary, str)):
            out += rules.p8(stage, name or "?", boundary)
    elif resource_type == ROLE_POLICY:
        policy = _json(props.get("policy"))
        role = props.get("role") if isinstance(props.get("role"), str) else None
        if policy is not None:
            out += p1_any(policy, policy_tags=role_kind(role), stage=stage_of(role), role_name=role)
    elif resource_type == POLICY:
        policy = _json(props.get("policy"))
        if policy is not None:
            role = _deployer_role_of_policy(name or "")
            out += p1_any(policy, policy_tags=role_kind(role, tags), stage=stage_of(name, tags), role_name=role)
    elif resource_type == FUNCTION:
        out += rules.p2({"name": name, "publish": props.get("publish")})
        env = ((props.get("environment") or {}).get("variables") or {}) if isinstance(props.get("environment"), Mapping) else {}
        out += rules.p4({k: v for k, v in env.items() if isinstance(v, str)})
    elif resource_type == LOG_GROUP:
        out += rules.p5(name or "?", props.get("retentionInDays"))
    elif resource_type == RUNTIME:
        env = props.get("environmentVariables") or {}
        out += rules.p4({k: v for k, v in env.items() if isinstance(v, str)})
        mode = ((props.get("networkConfiguration") or {}).get("networkMode")
                if isinstance(props.get("networkConfiguration"), Mapping) else None)
        stage = stage_of(props.get("agentRuntimeName", "").replace("_", "-") if isinstance(
            props.get("agentRuntimeName"), str) else None, tags)
        allow_public = (tags or {}).get("AllowPublicNetwork") == "true"
        if isinstance(mode, str):
            out += rules.p7(stage, mode, allow_public)
    out += tag_check(resource_type, props)
    return out


def tag_check(resource_type: str, props: Mapping[str, Any]) -> list[str]:
    """P3: every resource that takes tags carries Project and Stage."""
    if resource_type in UNTAGGED_OK or "tags" not in props and resource_type not in TAGGABLE:
        return []
    raw = props.get("tags")
    label = props.get("name") if isinstance(props.get("name"), str) else resource_type
    if raw is None and resource_type in TAGGABLE:
        return rules.p3(label, {})
    if resource_type.startswith(NATIVE_PREFIX) and isinstance(raw, list):
        return rules.p3(label, {t.get("key"): t.get("value") for t in raw if isinstance(t, Mapping)})
    if isinstance(raw, Mapping):
        return rules.p3(label, {k: v for k, v in raw.items() if isinstance(v, str)})
    return []


TAGGABLE = {
    ROLE, POLICY, FUNCTION, LOG_GROUP, RUNTIME, CODEBUILD,
    "aws:bedrock/agentcoreAgentRuntimeEndpoint:AgentcoreAgentRuntimeEndpoint",
    "aws:bedrock/agentcoreMemory:AgentcoreMemory", "aws:bedrock/guardrail:Guardrail",
    "aws:dynamodb/table:Table", "aws:s3/bucket:Bucket", "aws:ecr/repository:Repository",
    "aws:ssm/parameter:Parameter", "aws:apigateway/restApi:RestApi", "aws:apigateway/stage:Stage",
    "aws:apigateway/usagePlan:UsagePlan", "aws:apigateway/apiKey:ApiKey",
    "aws-native:bedrockagentcore:ConfigurationBundle",
}


# ---------------------------------------------------------------- stack level
def _runtime_invoke_resources(resources: Iterable[tuple[str, Mapping]]) -> tuple[list[str | None], bool]:
    """lambda:InvokeFunction resources of the runtime role's policy (role `m11-<stage>-runtime`)."""
    found: list[str | None] = []
    seen = False
    for rtype, props in resources:
        if rtype != ROLE_POLICY or not str(props.get("role", "")).endswith("-runtime"):
            continue
        policy = _json(props.get("policy"))
        if policy is None:
            found.append(None)
            continue
        seen = True
        for s in rules.statements(policy):
            if "lambda:InvokeFunction" in rules.as_list(s.get("Action")):
                found += [r if isinstance(r, str) else None for r in rules.as_list(s.get("Resource"))]
    return found, seen


def check_stack(resources: Sequence[tuple[str, Mapping]], retained: Sequence[str] | None = None) -> list[str]:
    out: list[str] = []
    invoke, _ = _runtime_invoke_resources(resources)
    functions = [p for t, p in resources if t == FUNCTION and str(p.get("name", "")).startswith("m11-")]
    stack_versions = [p.get("qualifiedArn") for p in functions if isinstance(p.get("qualifiedArn"), str)]
    pending = any(not isinstance(p.get("qualifiedArn"), str) for p in functions)
    if invoke:
        if retained is None:
            # Without the retained list the rule still requires qualified ARNs of the stage's own
            # tool functions; the exact set is re-checked after `up` (PinsViolation).
            own = [a for a in invoke if a and _QUALIFIED_OWN.match(a)]
            out += rules.p9(invoke, own, stack_versions, pending)
        else:
            out += rules.p9(invoke, retained, stack_versions, pending)
    projects = [p for t, p in resources if t == CODEBUILD]
    if projects:
        cache = [{"name": p.get("name"), "cache": p.get("cache") if isinstance(p.get("cache"), Mapping) else None}
                 for p in projects if isinstance(p.get("name"), str)]
        bucket = os.environ.get("M11_PIPELINE_BUCKET", "") or _pipeline_bucket(resources)
        role_policies = _project_role_policies(resources, projects)
        out += rules.p10([{**c, "cache": c["cache"] or {"type": "NO_CACHE"}} for c in cache], bucket, role_policies)
        out += _p11(resources, projects)
    return out


def _pipeline_bucket(resources: Sequence[tuple[str, Mapping]]) -> str:
    for rtype, props in resources:
        name = props.get("bucket")
        if rtype == BUCKET and isinstance(name, str) and name.startswith("m11-pipeline-artifacts-"):
            return name
    return ""


def _role_of(service_role: Any) -> str | None:
    return service_role.rsplit("/", 1)[-1] if isinstance(service_role, str) and ":role/" in service_role else None


def _policies_by_role(resources: Sequence[tuple[str, Mapping]]) -> dict[str, list]:
    """{role name: [statement lists of its inline policies]}; managed policies are keyed by their own name."""
    out: dict[str, list] = {}
    for rtype, props in resources:
        policy = _json(props.get("policy"))
        if policy is None:
            continue
        if rtype == ROLE_POLICY and isinstance(props.get("role"), str):
            out.setdefault(props["role"], []).extend(rules.statements(policy))
        elif rtype == POLICY and isinstance(props.get("name"), str):
            out.setdefault(props["name"], []).extend(rules.statements(policy))
    return out


def _project_role_policies(resources, projects) -> dict[str, list]:
    """{project name: its service role's inline statements} for P10's own-cache-prefix rule."""
    by_role = _policies_by_role(resources)
    out = {}
    for p in projects:
        role = _role_of(p.get("serviceRole"))
        if isinstance(p.get("name"), str) and role in by_role:
            out[p["name"]] = by_role[role]
    return out


def _p11(resources, projects) -> list[str]:
    """P11 on m11-platform: who may start m11-seal-experiment, the pipeline role's StartBuild list,
    and the two source types."""
    names = {p.get("name") for p in projects}
    if "m11-seal-experiment" not in names:
        return []
    arn = next((p.get("arn") for p in projects if p.get("name") == "m11-seal-experiment"), None)
    if not isinstance(arn, str):
        # During a first preview the project ARN is unknown; derive it from a sibling project's ARN.
        known = next((p.get("arn") for p in projects if isinstance(p.get("arn"), str)), None)
        if not isinstance(known, str):
            return []
        arn = known.rsplit("/", 1)[0] + "/m11-seal-experiment"
    prefix = arn.rsplit("/", 1)[0]
    pipeline_projects = [f"{prefix}/{n}" for n in ("m11-analyze", "m11-image", "m11-seal", "m11-deploy", "m11-gate",
                                                   "m11-bake")]
    source_types = {p["name"]: (p.get("source") or {}).get("type") for p in projects
                    if isinstance(p.get("name"), str) and isinstance(p.get("source"), Mapping)}
    return rules.p11(_policies_by_role(resources), arn, "m11-pipeline", pipeline_projects, source_types)


def retained_from_env(env: Mapping[str, str] = os.environ) -> list[str] | None:
    """retainedPins.toolArns from the release inputs `release deploy` passes (M11_RELEASE_INPUTS)."""
    path = env.get("M11_RELEASE_INPUTS")
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            pins = json.load(handle).get("retainedPins") or {}
    except (OSError, ValueError):
        return None
    arns = pins.get("toolArns") or []
    return sorted(arns.values()) if isinstance(arns, Mapping) else list(arns)
