# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure decisions of `release deploy`. The SDK side is module11/aws/deploy.py.

Everything here takes plain data: the retained pins and the refs they render with, the pins object
and `/applied`, the deployed manifest and its compact SSM form, the smoke verdict, and the
stage-wide restore plan after a failed smoke test (7a N5).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import manifest as M
from . import naming, pins as P

SMOKE_REQUIRED_STATUS = "completed"
RESTORE_KINDS = ("edge_code", "tool_permissions", "infrastructure")


class SmokeFailed(RuntimeError):
    pass


class SmokeFailedFlagsChanged(SmokeFailed):
    pass


class Frozen(RuntimeError):
    pass


class Latched(RuntimeError):
    pass


class SameBytesViolation(RuntimeError):
    pass


# ---------------------------------------------------------------- identity
def release_slug(git: Mapping[str, Any], stage: str) -> str:
    """`harness-v<n>` for a tagged release, `mr-<iid>` for a preview, else `operator-<commit7>`."""
    if naming.is_mr(stage):
        return stage
    tag = git.get("tag")
    if tag and naming.RELEASE_TAG.match(tag):
        return naming.release_slug(tag)
    return f"operator-{str(git.get('commit', '0000000'))[:7]}"


# ---------------------------------------------------------------- retained pins
def refs(stage: str, account: str, region: str, release_bucket: str) -> dict[str, str]:
    """Logical permission refs -> ARNs. Memory IDs are generated, so its ARN is the stage's
    name prefix (P1d position 4); everything else is literal."""
    artifacts = naming.artifacts_bucket(stage, account, region)
    return {"evidence": f"arn:aws:s3:::{release_bucket}/evidence/*",
            "snapshot": f"arn:aws:s3:::{release_bucket}/snapshots/policy/*",
            "side_effects": f"arn:aws:s3:::{artifacts}/side-effects/{stage}/*",
            "memory": f"arn:aws:bedrock-agentcore:{region}:{account}:memory/{naming.memory_name(stage)}-*"}


def static_pins(declared: Mapping, sealed: Mapping[str, Any], *, stage: str, account: str, region: str,
                release_bucket: str) -> P.Pins:
    """The candidate's pins known before `up`, read from the sealed pulumi-inputs.tar (M7)."""
    permissions = {name.split("/")[1]: doc for name, doc in sealed.items() if name.endswith("/permissions.json")}
    return P.static_pins(declared, sealed["models.yaml"], permissions, refs(stage, account, region, release_bucket),
                         region, account)


def retained(current_own: P.Pins | None, static: P.Pins) -> P.Pins:
    return P.union(*(p for p in (current_own, static) if p is not None))


def own_pins(static: P.Pins, effective: Mapping[str, Any]) -> P.Pins:
    """This release's own pins: its tool versions and guardrail version (from `effectivePins`), its
    models, statements and deadline (static)."""
    return P.Pins(toolArns=dict(effective["ownToolArns"]), modelResources=list(static.modelResources),
                  guardrailVersion=str(effective["guardrailVersion"]), deadlineSeconds=static.deadlineSeconds,
                  toolStatements=dict(static.toolStatements))


def pins_object(stage: str, manifest_version: str, own: P.Pins, effective: Mapping[str, Any],
                retained_pins: P.Pins, deployed_tool_arns: Mapping[str, str]) -> dict:
    """{own, granted} checked by the PinsViolation rule before anything is published:
    granted holds only retained pins and this stack's versions, and own == the `toolArns` output."""
    granted = P.Pins.from_dict(effective["granted"])
    P.check_after_up(granted, retained_pins, effective["ownToolArns"], own, deployed_tool_arns)
    return P.pins_object(stage, manifest_version, own, granted)


def applied_doc(*, inputs_uri: str, inputs_sha: str, manifest_version: str, applied_at: str,
                policy_digests: Mapping[str, str], pins_uri: str) -> dict:
    return {"releaseInputsUri": inputs_uri, "sha256": inputs_sha, "manifestVersion": manifest_version,
            "appliedAt": applied_at, "policyDigests": dict(sorted(policy_digests.items())), "effectivePinsUri": pins_uri}


# ---------------------------------------------------------------- the deployed manifest
def deployed_manifest(declared: Mapping, stage: str, deployment: Mapping[str, Any],
                      previous: Mapping | None = None) -> dict:
    """The declared manifest plus `deployment.<stage>`; other stages' sections are not carried."""
    out = json.loads(json.dumps(declared))
    out["deployment"] = {stage: dict(deployment)}
    M.validate(out)
    return out


def compact_current(full: Mapping, s3_uri: str) -> dict:
    return M.compact(dict(full), s3_uri)


# ---------------------------------------------------------------- smoke
_EVENT = re.compile(r"^event: (?P<name>[^\n]+)\ndata: (?P<data>.*)$", re.M)


def sse_events(text: str) -> list[tuple[str, Any]]:
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        match = _EVENT.match(block.strip())
        if match:
            try:
                out.append((match.group("name"), json.loads(match.group("data"))))
            except ValueError:
                out.append((match.group("name"), match.group("data")))
    return out


@dataclass
class SmokeVerdict:
    ok: bool
    status: str
    executed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    detail: str | None = None


def smoke_verdict(stream_text: str, required: Iterable[str]) -> SmokeVerdict:
    """status=completed with every required tool executed (the final `result` event)."""
    events = sse_events(stream_text)
    final = next((data for name, data in reversed(events) if name in {"result", "error"}), None)
    if not isinstance(final, Mapping):
        return SmokeVerdict(False, "no_result", detail="the stream ended without a result event")
    executed = sorted({e.get("tool") for e in final.get("events") or [] if e.get("status") == "completed"})
    missing = sorted(set(required) - set(executed))
    ok = final.get("status") == SMOKE_REQUIRED_STATUS and not missing
    return SmokeVerdict(ok, str(final.get("status")), executed, missing, final.get("detail") or final.get("error"))


def smoke_envelope(record: Mapping, release: str, request_id: str) -> dict:
    smoke = record["smoke"]
    return {"identity": {"team": smoke.get("team") or "platform", "principal": "m11-smoke"},
            "request": {"change_summary": smoke["input"], "request_id": request_id},
            "context": {"variant": "eval", "shadow": True, "scenarioId": smoke["scenarioId"],
                        "manifestVersion": record["manifestVersion"], "behaviorDigest": record["behaviorDigest"],
                        "release": release}}


def restore_plan(effective_kinds: Iterable[str], *, flags_written: bool, has_current: bool) -> list[str]:
    """What a failed smoke test restores before exiting (7a N5): ['flags', 'reapply'] subsets."""
    if not has_current:
        return []
    plan = ["flags"] if flags_written else []
    if set(effective_kinds) & set(RESTORE_KINDS):
        plan.append("reapply")
    return plan
