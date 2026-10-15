# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""IAM statement matching, pure (the mr-* boundary tests, policy rules P1d and P11).

- `covers(boundary, statement)`: which (action, resource) pairs of an Allow statement the
  boundary's Allows do not cover (action and resource globbing, condition containment).
- `grants(statement, action, resource)`: does an Allow statement grant this action on this
  resource, counting `NotAction` / `NotResource`.
- `resource_wildcard_ok(arn, statement, policy_tags, stagekey, ...)`: the single P1d matcher for
  a `*` inside a Resource ARN (positions 1-6).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

PROJECT = "m11-devops-companion"
# The roles that deploy `mr-*` stacks: the trusted preview build, its MR-tooling variant, and the
# operator's preview deployer (before the pipeline exists).
PREVIEW_ROLES = ("m11-preview", "m11-preview-infra", "m11-preview-deployer")
_MR_KEY = re.compile(r"^[a-z0-9-]+/mr-\*$")
_MR_PARAMETER = re.compile(r"^arn:aws:ssm:[a-z0-9-]+:\d{12}:parameter/m11/devops-companion/mr-\*$")
_ANY_RUNTIME = re.compile(r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:"
                          r"(runtime|memory|evaluator|configuration-bundle|gateway|"
                          r"workload-identity-directory/default/workload-identity)/\*$")


def as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def statements(policy: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[dict]:
    if isinstance(policy, Mapping):
        return [dict(s) for s in as_list(policy.get("Statement"))]
    return [dict(s) for s in policy]


# ---------------------------------------------------------------- glob matching
@lru_cache(maxsize=4096)
def _regex(pattern: str, case_insensitive: bool) -> re.Pattern:
    body = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.compile(f"^{body}$", re.IGNORECASE if case_insensitive else 0)


def glob_match(pattern: str, value: str, case_insensitive: bool = False) -> bool:
    return bool(_regex(pattern, case_insensitive).match(value))


def pattern_contains(outer: str, inner: str, case_insensitive: bool = False) -> bool:
    """True when every string matching `inner` also matches `outer` ('*' and '?' wildcards).
    The inner pattern's wildcards are symbols only an outer wildcard can absorb."""
    if case_insensitive:
        outer, inner = outer.lower(), inner.lower()

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        if i == len(outer):
            return j == len(inner)
        ch = outer[i]
        if ch == "*":
            return go(i + 1, j) or (j < len(inner) and go(i, j + 1))
        if j == len(inner):
            return False
        if ch == "?":
            return inner[j] != "*" and go(i + 1, j + 1)
        return inner[j] == ch and go(i + 1, j + 1)

    return go(0, 0)


# ---------------------------------------------------------------- conditions
def _cond_items(condition: Mapping[str, Any] | None) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for op, block in (condition or {}).items():
        for key, values in (block or {}).items():
            out[(op, key.lower())] = {str(v) for v in as_list(values)}
    return out


def condition_contains(outer: Mapping[str, Any] | None, inner: Mapping[str, Any] | None) -> bool:
    """The inner statement is at least as restricted: every outer condition (operator, key) is also
    in the inner one, with the inner values a subset of the outer values."""
    o, i = _cond_items(outer), _cond_items(inner)
    for k, values in o.items():
        if k not in i or not i[k] <= values:
            return False
    return True


# ---------------------------------------------------------------- coverage and grants
def _allows(stmts: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [s for s in stmts if s.get("Effect", "Allow") == "Allow"]


def covers(boundary: Mapping[str, Any] | Sequence[Mapping[str, Any]], statement: Mapping[str, Any]) -> list[str]:
    """Uncovered 'action resource' pairs of an Allow statement under the boundary (empty when covered)."""
    if statement.get("Effect", "Allow") != "Allow":
        return []
    allows = _allows(statements(boundary))
    missing = []
    for action in as_list(statement.get("Action")):
        for resource in as_list(statement.get("Resource")):
            if not any(_covered_by(b, action, resource, statement.get("Condition")) for b in allows):
                missing.append(f"{action} {resource}")
    return missing


def _covered_by(b: Mapping[str, Any], action: str, resource: str, condition: Mapping | None) -> bool:
    if "NotAction" in b or "NotResource" in b:
        return False          # the boundary uses neither; refuse to reason about them
    if not any(pattern_contains(p, action, case_insensitive=True) for p in as_list(b.get("Action"))):
        return False
    if not any(pattern_contains(p, resource) for p in as_list(b.get("Resource"))):
        return False
    return condition_contains(b.get("Condition"), condition)


def grants(statement: Mapping[str, Any], action: str, resource: str) -> bool:
    """Whether an Allow statement's action and resource sets include this action and resource,
    ignoring conditions (P11 counts any such statement as a grant)."""
    if statement.get("Effect", "Allow") != "Allow":
        return False
    if "NotAction" in statement:
        act = not any(glob_match(p, action, True) for p in as_list(statement["NotAction"]))
    else:
        act = any(glob_match(p, action, True) for p in as_list(statement.get("Action")))
    if "NotResource" in statement:
        res = not any(glob_match(p, resource) for p in as_list(statement["NotResource"]))
    else:
        res = any(glob_match(p, resource) for p in as_list(statement.get("Resource")))
    return act and res


# ---------------------------------------------------------------- P1d matcher
_S3_OBJECT = re.compile(r"^arn:aws:s3:::(?P<bucket>[^/]+)/(?P<key>.*)$")
_LOG_GROUP = re.compile(r"^arn:aws:logs:[a-z0-9-]+:\d{12}:log-group:(?P<name>[^:]+):\*$")
_LATCH = re.compile(r"^arn:aws:ssm:[a-z0-9-]+:\d{12}:parameter/m11/devops-companion/latch/\*$")
_GENERATED = re.compile(r"^arn:aws:apigateway:[a-z0-9-]+::/(?P<kind>restapis|usageplans|apikeys|tags)/\*$")
_GUARDRAIL = re.compile(r"^arn:aws:bedrock:[a-z0-9-]+:\d{12}:guardrail/\*$")
_BOUNDARY_ONLY = re.compile(r"^arn:aws:bedrock:[a-z0-9*-]*:(\d{12})?:(foundation-model|inference-profile)/\*$")
_EVENT_SOURCE_MAPPING = re.compile(r"^arn:aws:lambda:[a-z0-9-]+:\d{12}:event-source-mapping:\*$")
_BUILTIN_EVALUATOR = "arn:aws:bedrock-agentcore:::evaluator/Builtin.*"
_BUILTIN_EVALUATOR_REGIONAL = re.compile(r"^arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:evaluator/Builtin\.\*$")


def _tags(policy_tags: Mapping[str, str] | None) -> dict[str, str]:
    return dict(policy_tags or {})


def _is(policy_tags: Mapping[str, str] | None, role: str) -> bool:
    return _tags(policy_tags).get("M11Role") == role


def _stage_condition_ok(statement: Mapping[str, Any], stage: str, preview: bool, key: str) -> bool:
    cond = statement.get("Condition") or {}
    if preview:
        return str((cond.get("StringLike") or {}).get(key)) == "mr-*"
    return str((cond.get("StringEquals") or {}).get(key)) == stage


def _request_tag_conditions(statement: Mapping[str, Any], stage: str, preview: bool) -> bool:
    cond = statement.get("Condition") or {}
    stage_ok = (str((cond.get("StringLike") or {}).get("aws:RequestTag/Stage")) == "mr-*") if preview else \
        (str((cond.get("StringEquals") or {}).get("aws:RequestTag/Stage")) == stage)
    project_ok = str((cond.get("StringEquals") or {}).get("aws:RequestTag/Project")) == PROJECT
    keys = (cond.get("ForAllValues:StringEquals") or {}).get("aws:TagKeys")
    keys_ok = keys is not None and set(as_list(keys)) <= {"Project", "Stage"}
    return stage_ok and project_ok and keys_ok


def _has_deny(policy: Sequence[Mapping[str, Any]], sid: str) -> bool:
    return any(s.get("Effect") == "Deny" and s.get("Sid") == sid for s in policy)


def _foreign_stage_deny(policy: Sequence[Mapping[str, Any]], arn: str, stage: str, preview: bool) -> bool:
    """The explicit resource-scoped Deny: same resource, ResourceTag/Stage not <S>."""
    op, value = ("StringNotLike", "mr-*") if preview else ("StringNotEquals", stage)
    for s in policy:
        if s.get("Effect") != "Deny" or arn not in as_list(s.get("Resource")):
            continue
        if str(((s.get("Condition") or {}).get(op) or {}).get("aws:ResourceTag/Stage")) == value:
            return True
    return False


def resource_wildcard_ok(arn: str, statement: Mapping[str, Any], policy_tags: Mapping[str, str] | None,
                         stagekey: str, *, stage: str | None = None, role_name: str | None = None,
                         policy: Sequence[Mapping[str, Any]] | None = None) -> bool:
    """P1d: is this `*` inside a Resource ARN in an allowed position? `Resource: "*"` is P1b's."""
    if "*" not in arn or arn == "*":
        return True
    actions = [a.lower() for a in as_list(statement.get("Action"))]
    deployer, boundary = _is(policy_tags, "deployer"), _is(policy_tags, "boundary")
    stage = stage or stagekey
    preview = role_name in PREVIEW_ROLES
    policy = list(policy or [statement])

    # (5) AWS-owned or generated IDs with no name.
    if arn == _BUILTIN_EVALUATOR or _BUILTIN_EVALUATOR_REGIONAL.match(arn):
        return actions == ["bedrock-agentcore:evaluate"]
    if _BOUNDARY_ONLY.match(arn):
        return boundary
    # (6) Lambda event source mappings (generated UUIDs): deployers only, scoped by lambda:FunctionArn to the
    # statement's own stage functions.
    if _EVENT_SOURCE_MAPPING.match(arn):
        if deployer and actions == ["lambda:listtags"]:
            return True          # read-only: the provider's tag read, which has no lambda:FunctionArn key
        target = str(((statement.get("Condition") or {}).get("ArnLike") or {}).get("lambda:FunctionArn", ""))
        return deployer and bool(re.search(rf":function:m11-{re.escape(stagekey)}-\*$", target))
    # (6) generated-ID resources managed by a deployer, and (5) the boundary's tagged guardrails.
    generated = _GENERATED.match(arn)
    if _GUARDRAIL.match(arn):
        cond = statement.get("Condition") or {}
        if boundary:
            return str((cond.get("StringEquals") or {}).get("aws:ResourceTag/Project")) == PROJECT and \
                str((cond.get("StringLike") or {}).get("aws:ResourceTag/Stage")) == "mr-*"
        if not deployer:
            return False
        if actions == ["bedrock:tagresource"] and not _stage_condition_ok(statement, stage, preview,
                                                                         "aws:ResourceTag/Stage"):
            # The create-time TagResource: request-tag conditions and all three tag Denies; never preview-infra.
            return (role_name != "m11-preview-infra" and _request_tag_conditions(statement, stage, preview)
                    and all(_has_deny(policy, sid) for sid in ("DenyForeignStageTagging",
                                                               "DenyRetagOtherStageGuardrail",
                                                               "DenyRetagForeignProjectGuardrail")))
        if not _stage_condition_ok(statement, stage, preview, "aws:ResourceTag/Stage"):
            return False
        if {"bedrock:tagresource", "bedrock:untagresource"} & set(actions):
            return _has_deny(policy, "DenyForeignStageTagging")
        return _foreign_stage_deny(policy, arn, stage, preview)
    if generated:
        if not deployer:
            return False
        if generated.group("kind") == "restapis" and actions == ["apigateway:get"] and \
                (statement.get("Condition") or {}) == {"Null": {"aws:ResourceTag/Stage": "true"}}:
            return True          # read-only, untagged (deleted) APIs only: the provider's delete wait
        if generated.group("kind") == "tags":
            return _has_deny(policy, "DenyForeignStageTagging")
        return _stage_condition_ok(statement, stage, preview, "aws:ResourceTag/Stage") and \
            _foreign_stage_deny(policy, arn, stage, preview)
    # (1) S3 objects in a literal m11 bucket.
    s3 = _S3_OBJECT.match(arn)
    if s3:
        bucket, key = s3.group("bucket"), s3.group("key")
        if "*" in bucket:
            return (deployer or boundary) and bucket.startswith("m11-")
        if preview and deployer and bucket.startswith("m11-") and _MR_KEY.match(key):
            return True           # `release-inputs/mr-*`, `ledger/mr-*`: every mr-* stage, preview deployers only
        if not bucket.startswith("m11-") or not key.endswith("*") or "*" in key[:-1]:
            return False
        prefix = key[:-1]
        if prefix and not prefix.endswith("/"):
            return False
        if prefix.startswith("ledger/") or prefix == "ledger/":
            return prefix == f"ledger/{stagekey}/"
        return True
    # (2) the log-stream suffix of a literal log group.
    group = _LOG_GROUP.match(arn)
    if group:
        return "*" not in group.group("name")
    # (3) the latch parameters, controller role only.
    if _LATCH.match(arn):
        # The controller writes latches; a deployer reads them (release deploy step 1).
        return _is(policy_tags, "controller") or (deployer and actions == ["ssm:getparameter"])
    if _MR_PARAMETER.match(arn):
        return preview and deployer   # /m11/devops-companion/mr-<n>/{current,applied,...}
    if _ANY_RUNTIME.match(arn):
        # CreateAgentRuntime authorizes its DEFAULT endpoint against the literal `runtime/*` (generated
        # ID, position 6): deployers only, CreateAgentRuntimeEndpoint only, with the stage's request tags.
        if (deployer or _is(policy_tags, "drift-checker")) and arn.endswith(":configuration-bundle/*") and \
                actions == ["bedrock-agentcore:listconfigurationbundleversions"]:
            # Read-only; the service authorizes it against configuration-bundle/* even for one bundle (probed
            # 2026-10-08 with an exact-ARN session: AccessDenied). The drift checker lists its bundle's versions.
            return True
        cond = statement.get("Condition") or {}
        stage_tag = ((cond.get("StringLike") or {}) if preview else (cond.get("StringEquals") or {})).get(
            "aws:RequestTag/Stage")
        allowed = {"bedrock-agentcore:createagentruntimeendpoint", "bedrock-agentcore:tagresource",
                   "bedrock-agentcore:createworkloadidentity"}
        return (deployer and bool(actions) and set(actions) <= allowed
                and str(stage_tag) == ("mr-*" if preview else stage)
                and str((cond.get("StringEquals") or {}).get("aws:RequestTag/Project")) == PROJECT)
    # (4) name prefixes: the statement's own stage key, or a broader m11 prefix for deployers/the boundary.
    head = arn.split("*", 1)[0]
    tail = re.split(r"[/:]", head)[-1]       # the resource-name prefix in front of the first '*'
    if re.match(rf"^m11[-_]{re.escape(stagekey)}([-_]|$)", tail):
        return True
    if tail.startswith(("m11-", "m11_")):
        return deployer or boundary
    return False
