# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retained pins: shared IAM keeps every serving version working.

Each release records what it pins itself (`own`) and what the policies grant after its
`pulumi up` (`granted`). Only `own` is ever carried forward, so grants never accumulate.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

PINS_SCHEMA = "m11.pins/2"
SERVING_STATES = {"IDLE", "PROMOTED", "ROLLED_BACK"}
MAX_RUNTIME_POLICY_CHARS = 8000

# Logical permission refs: the actions each may grant. Shared with check C6.
REF_ACTIONS = {
    "evidence": {"s3:GetObject"},
    "snapshot": {"s3:GetObject"},
    "side_effects": {"s3:PutObject"},
    "memory": {"bedrock-agentcore:BatchCreateMemoryRecords", "bedrock-agentcore:ListMemoryRecords"},
}


class PinsViolation(RuntimeError):
    pass


class PruneUnsafe(RuntimeError):
    pass


class RolloutInProgress(RuntimeError):
    pass


@dataclass
class Pins:
    toolArns: Any = field(default_factory=dict)          # own: {tool: arn}; granted: [arn]
    modelResources: list[str] = field(default_factory=list)
    guardrailVersion: str | None = None
    deadlineSeconds: int | None = None
    toolStatements: dict[str, list[dict]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"toolArns": self.toolArns, "modelResources": list(self.modelResources),
               "deadlineSeconds": self.deadlineSeconds, "toolStatements": self.toolStatements}
        if self.guardrailVersion is not None:
            out["guardrailVersion"] = self.guardrailVersion
        return out

    @classmethod
    def from_dict(cls, doc: Mapping) -> "Pins":
        return cls(toolArns=doc.get("toolArns") or {}, modelResources=list(doc.get("modelResources") or []),
                   guardrailVersion=doc.get("guardrailVersion"), deadlineSeconds=doc.get("deadlineSeconds"),
                   toolStatements=dict(doc.get("toolStatements") or {}))

    def tool_arn_set(self) -> set[str]:
        return set(self.toolArns.values()) if isinstance(self.toolArns, dict) else set(self.toolArns)


# ---------------------------------------------------------------- statements
def least_privilege(permissions: list[dict], refs: Mapping[str, str]) -> list[dict]:
    """Data statements for one tool's permissions.json; refs map a logical ref to its ARN."""
    statements = []
    for entry in permissions:
        ref = entry["resource"]
        actions = sorted(entry["actions"])
        bad = [a for a in actions if "*" in a or a not in REF_ACTIONS.get(ref, set())]
        if bad:
            raise PinsViolation(f"actions {bad} are not allowed on ref {ref}")
        resource = refs[ref]
        if entry.get("pathPrefix"):
            resource = resource.rstrip("*").rstrip("/") + "/" + entry["pathPrefix"].strip("/") + "/*"
        statements.append({"Effect": "Allow", "Action": actions, "Resource": [resource]})
    return statements


def model_resources(models: Mapping[str, Mapping], region: str, account: str, partition: str = "aws") -> list[str]:
    """Bedrock resources for models.yaml: base model (any region), profile, project."""
    out = set()
    for spec in models.values():
        profile = spec["modelId"]
        base = spec.get("foundationModelId") or profile.split(".", 1)[-1]
        out.add(f"arn:{partition}:bedrock:*::foundation-model/{base}")
        out.add(f"arn:{partition}:bedrock:{region}:{account}:inference-profile/{profile}")
    out.add(f"arn:{partition}:bedrock:{region}:{account}:project/default")
    return sorted(out)


def _dedupe(statements: list[dict]) -> list[dict]:
    seen, out = set(), []
    for s in statements:
        key = json.dumps(s, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# ---------------------------------------------------------------- the four pin functions
def pins_of(deployed_manifest: Mapping, stage: str, read_object: Callable[[str], Mapping]) -> Pins:
    """`own` pins of a deployed manifest (`/current` or `/candidate`), via its pinsUri. Never `granted`."""
    uri = ((deployed_manifest.get("deployment") or {}).get(stage) or {}).get("pinsUri")
    if not uri:
        raise PinsViolation(f"deployed manifest has no deployment.{stage}.pinsUri")
    doc = read_object(uri)
    if doc.get("schema") != PINS_SCHEMA:
        raise PinsViolation(f"{uri} is not a {PINS_SCHEMA} object")
    return Pins.from_dict(doc["own"])


def static_pins(declared_manifest: Mapping, models: Mapping, permissions: Mapping[str, list[dict]],
                refs: Mapping[str, str], region: str, account: str) -> Pins:
    """The candidate's pins known before `up`: model resources, tool statements, deadline."""
    return Pins(toolArns={}, modelResources=model_resources(models, region, account),
                deadlineSeconds=int(declared_manifest["orchestration"]["deadlineSeconds"]),
                toolStatements={t: least_privilege(p, refs) for t, p in sorted(permissions.items()) if p})


def union(*pins: Pins) -> Pins:
    """retainedPins = union(pins_of(/current), static_pins(declared)); deadline is the maximum."""
    arns: set[str] = set()
    models: set[str] = set()
    statements: dict[str, list[dict]] = {}
    deadlines = []
    for p in pins:
        arns |= p.tool_arn_set()
        models |= set(p.modelResources)
        for tool, sts in p.toolStatements.items():
            statements[tool] = _dedupe(statements.get(tool, []) + list(sts))
        if p.deadlineSeconds is not None:
            deadlines.append(p.deadlineSeconds)
    return Pins(toolArns=sorted(arns), modelResources=sorted(models),
                deadlineSeconds=max(deadlines) if deadlines else None, toolStatements=statements)


def granted(retained: Pins, stack_tool_arns: Mapping[str, str], guardrail_version: str | None = None) -> Pins:
    """What the rendered policies allow after `up`: retainedPins plus this stack's own Function versions."""
    g = union(retained, Pins(toolArns=dict(stack_tool_arns)))
    g.guardrailVersion = guardrail_version
    return g


def prune(current_own: Pins, stack_tool_arns: Mapping[str, str]) -> Pins:
    """`release prune`: retainedPins = /current's own, plus the stack's Function outputs."""
    return granted(Pins(toolArns=dict(current_own.toolArns), modelResources=list(current_own.modelResources),
                        deadlineSeconds=current_own.deadlineSeconds, toolStatements=dict(current_own.toolStatements)),
                   stack_tool_arns)


def pins_object(stage: str, manifest_version: str, own: Pins, granted_pins: Pins) -> dict:
    return {"schema": PINS_SCHEMA, "stage": stage, "manifestVersion": manifest_version,
            "own": own.to_dict(), "granted": granted_pins.to_dict()}


# ---------------------------------------------------------------- guards
def check_after_up(granted_pins: Pins, retained: Pins, stack_tool_arns: Mapping[str, str],
                   own: Pins, deployed_tool_arns: Mapping[str, str]) -> None:
    """Post-`up` PinsViolation rule: granted only retained + own-stack versions; own == deployed."""
    allowed = retained.tool_arn_set() | set(stack_tool_arns.values())
    extra = sorted(granted_pins.tool_arn_set() - allowed)
    if extra:
        raise PinsViolation(f"granted tool ARNs outside retained and own-stack versions: {extra}")
    if dict(own.toolArns) != dict(deployed_tool_arns):
        raise PinsViolation("own != deployed")


def check_prune_safe(current_runtime_version: str, endpoint_versions: Mapping[str, str],
                     control_endpoints: Mapping[str, Mapping]) -> None:
    other = {name: v for name, v in endpoint_versions.items() if str(v) != str(current_runtime_version)}
    other.update({f"control:{n}": e.get("version") for n, e in control_endpoints.items()
                  if str(e.get("version")) != str(current_runtime_version)})
    if other:
        raise PruneUnsafe(f"endpoints reference versions other than /current's {current_runtime_version}: {other}")


def check_rollout_idle(control_state: str | None) -> None:
    if control_state not in SERVING_STATES and control_state is not None:
        raise RolloutInProgress(f"rollout state is {control_state}; wait for PROMOTED or ROLLED_BACK")


def adapter_timeout(retained: Pins) -> int:
    """The stage-wide adapter Lambda timeout: retainedPins.deadlineSeconds + 30."""
    return int(retained.deadlineSeconds or 0) + 30


MAX_DEADLINE_SECONDS = 86_400


def ts(value: Any) -> datetime:
    """Aware datetime (UTC for epoch input) from epoch seconds (int, float or Decimal: the control
    item), an ISO 8601 string with an offset or Z (demo-state.json), or an aware datetime.
    Anything else, None included, raises PinsViolation."""
    if isinstance(value, bool):
        raise PinsViolation(f"not a timestamp: {value!r}")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise PinsViolation("naive datetime")
        return value
    try:
        if isinstance(value, (int, float, Decimal)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise PinsViolation(f"timestamp without offset: {value!r}")
            return parsed
    except (ValueError, OverflowError, OSError, ArithmeticError) as exc:  # NaN, out of range, unparseable text
        raise PinsViolation(f"not a timestamp: {value!r} ({exc})") from exc
    raise PinsViolation(f"not a timestamp: {value!r}")


def granted_deadline_seconds(candidate_pins: Mapping) -> int:
    """granted.deadlineSeconds of the candidate's own pins object, as a positive int <= 86,400
    (a non-mapping `granted` or an absurd value is PinsViolation)."""
    granted_doc = candidate_pins.get("granted") if isinstance(candidate_pins, Mapping) else None
    if granted_doc is None:
        granted_doc = {}
    if not isinstance(granted_doc, Mapping):
        raise PinsViolation(f"candidate pins object's granted is not a mapping: {type(granted_doc).__name__}")
    v = granted_doc.get("deadlineSeconds")
    if (isinstance(v, bool) or not isinstance(v, (int, Decimal))
            or (isinstance(v, Decimal) and (not v.is_finite() or v != v.to_integral_value()))
            or int(v) <= 0 or int(v) > MAX_DEADLINE_SECONDS):
        raise PinsViolation(f"candidate pins object has no positive integer granted.deadlineSeconds "
                            f"(<= {MAX_DEADLINE_SECONDS}): {v!r}")
    return int(v)


def _plus(at: datetime, seconds: int) -> datetime:
    try:
        return at + timedelta(seconds=seconds)
    except OverflowError as exc:  # a terminal timestamp near datetime.max
        raise PinsViolation(f"timestamp {at.isoformat()} + {seconds} s is out of range") from exc


def purge_ready_at(rollout: Mapping, candidate_pins: Mapping, driver: Mapping | None = None) -> datetime | None:
    """The time a terminal rollout's records may be purged (the purge-wait rule);
    None = keep waiting (this rollout's traffic driver is still running). Pure.

    1. this rollout's driver state (`traffic.rolloutId` == the rollout's) has `finishedAt`: that time
    2. it has `killedAt`: killedAt + deadlineSeconds + 30 s
    3. it has neither: None
    4. no driver state for this rollout: the terminal time (rolledBackAt, or promotedAt for a PROMOTED
       rollout; epoch seconds) + deadlineSeconds + 30 s
    deadlineSeconds is the rolled-back candidate's own granted.deadlineSeconds. Cases 1 and 2 also need
    the driver's `startedAt`, and a finishedAt/killedAt earlier than it is PinsViolation."""
    deadline = granted_deadline_seconds(candidate_pins)  # PinsViolation if missing or not a positive int, in every case
    if driver and driver.get("rolloutId") and driver["rolloutId"] == rollout.get("rolloutId"):
        if driver.get("finishedAt") or driver.get("killedAt"):
            if not driver.get("startedAt"):
                raise PinsViolation("traffic driver record has no startedAt")
            started = ts(driver["startedAt"])
            key = "finishedAt" if driver.get("finishedAt") else "killedAt"
            at = ts(driver[key])
            if at < started:
                raise PinsViolation(f"traffic.{key} {at.isoformat()} is earlier than traffic.startedAt "
                                    f"{started.isoformat()}")
            return at if key == "finishedAt" else _plus(at, deadline + 30)
        return None
    terminal_at = rollout.get("rolledBackAt") or rollout.get("promotedAt")  # epoch seconds; None -> PinsViolation
    return _plus(ts(terminal_at), deadline + 30)


# ---------------------------------------------------------------- rendered runtime role (size guard)
def runtime_role_policy(granted_pins: Pins, stack_tool_arns: Mapping[str, str], log_group_arn: str) -> dict:
    """The runtime role's policy shape: invoke = granted ∪ own Functions; Bedrock = models."""
    invoke = sorted(granted_pins.tool_arn_set() | set(stack_tool_arns.values()))
    statements = [
        {"Sid": "InvokeTools", "Effect": "Allow", "Action": ["lambda:InvokeFunction"], "Resource": invoke},
        {"Sid": "InvokeModels", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": list(granted_pins.modelResources)},
        {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": [f"{log_group_arn}:*"]},
    ]
    return {"Version": "2012-10-17", "Statement": [s for s in statements if s["Resource"]]}


def policy_chars(policy: Mapping) -> int:
    return len(json.dumps(policy, separators=(",", ":")))
