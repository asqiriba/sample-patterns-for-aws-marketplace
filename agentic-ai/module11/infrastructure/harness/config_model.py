# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Typed configuration for m11-harness. Pure: no Pulumi, no AWS.

`Pulumi.<stack>.yaml` holds stage settings only (validated by `settings()`); everything a release
changes arrives as `ReleaseInputs` (M11_RELEASE_INPUTS, else `/applied`, else
ReleaseInputsMissing). Behavior files are read only from the sealed `pulumi-inputs.tar` (M7).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from module11.release import naming
from module11.release.pulumi_inputs import ReleaseInputs, ReleaseInputsMissing, load as load_inputs  # noqa: F401

MAX_RUNTIME_CONFIG_CHARS = 4800       # AgentCore caps an env value at 5,000 characters
PROFILES = ("demo", "prod", "none")
ENDPOINTS = {"dev": ["stable", "candidate"], "prod": ["stable", "candidate"], "staging": ["staging"]}
USAGE_PLAN = {"limit": 5000, "period": "DAY", "rateLimit": 25, "burstLimit": 50}
STAGE_THROTTLE = {"rateLimit": 30, "burstLimit": 60}

# Stage defaults. Values that differ from the production default for the demo are labelled, and
# preflight prints them: "Demo settings: team concurrency 14 (default 10), online evaluation 100%
# (default 10%)." dev is prod-shaped on purpose (candidate endpoint, rollout profile demo).
STAGE_DEFAULTS: dict[str, dict] = {
    "dev": {"teamConcurrency": 14, "rolloutProfile": "demo", "onlineEval": {"enabled": True, "samplingPct": 10},
            "shadow": True, "allowPublicNetwork": True, "accessLogs": True,
            "demoLabels": {"teamConcurrency": "demo value (default 3): the S7 traffic shape's 12-worker pool",
                           "rolloutProfile": "demo (dev is prod-shaped for the canary rehearsal)"}},
    # staging traffic is gates (variant eval, never scored) and the Gateway A/B test, which needs every session
    # scored to reach significance on ~200 sessions.
    "staging": {"teamConcurrency": 3, "rolloutProfile": "none", "onlineEval": {"enabled": True, "samplingPct": 100},
                "shadow": False, "allowPublicNetwork": True, "accessLogs": True,
                "demoLabels": {"onlineEval.samplingPct": "demo value (default 10): the A/B test scores every session"}},
    "prod": {"teamConcurrency": 14, "rolloutProfile": "demo", "onlineEval": {"enabled": True, "samplingPct": 100},
             "shadow": True, "allowPublicNetwork": True, "accessLogs": True,
             "demoLabels": {"teamConcurrency": "demo value (default 10)", "rolloutProfile": "demo (default prod)",
                            "onlineEval.samplingPct": "demo value (default 10)",
                            "allowPublicNetwork": "demo relaxation: PUBLIC network mode as in Module 10 (P7)"}},
    "mr": {"teamConcurrency": 3, "rolloutProfile": "none", "onlineEval": {"enabled": False, "samplingPct": 0},
           "shadow": False, "allowPublicNetwork": True, "accessLogs": False, "uiPreview": "pending"},
}


class ConfigError(ValueError):
    pass


class RuntimeConfigTooLarge(ValueError):
    pass


class BootstrapNotAllowed(ValueError):
    pass


class BundleArnMismatch(ValueError):
    pass


class ApplyRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class StageSettings:
    stage: str
    accountId: str
    region: str
    endpoints: list[str]
    teamConcurrency: int
    rolloutProfile: str
    onlineEval: dict
    shadow: bool
    allowPublicNetwork: bool
    operatorPrincipals: list[str]
    uiOrigin: str | None = None
    accessLogs: bool = True
    usagePlan: dict = field(default_factory=lambda: dict(USAGE_PLAN))
    previewBranch: str | None = None
    uiPreview: str | None = None
    demoLabels: dict = field(default_factory=dict)

    @property
    def stagekey(self) -> str:
        return naming.stagekey(self.stage)

    @property
    def is_mr(self) -> bool:
        return naming.is_mr(self.stage)

    def to_dict(self) -> dict:
        return asdict(self)


def defaults_for(stage: str) -> dict:
    naming.check_stage(stage)
    base = STAGE_DEFAULTS["mr" if naming.is_mr(stage) else stage]
    return json.loads(json.dumps(base))


_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:(user|role)/[\w+=,.@/-]+$")
_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(:\d+)?$")


def settings(get: Callable[[str], Any], *, stack: str) -> StageSettings:
    """Validate the stage settings read from Pulumi config (`get(key)` returns the decoded value or None)."""
    stage = get("stage") or stack
    if stage != stack:
        raise ConfigError(f"stage {stage!r} differs from the stack name {stack!r}")
    try:
        naming.check_stage(stage)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    doc = {**defaults_for(stage), **{k: v for k in StageSettings.__dataclass_fields__ if (v := get(k)) is not None}}
    doc["stage"] = stage
    if isinstance(doc.get("accountId"), int):          # Pulumi config values are strings; JSON decoding makes 12 digits an int
        doc["accountId"] = f"{doc['accountId']:012d}"
    doc.setdefault("endpoints", list(naming.endpoints_for(stage)))
    for key in ("accountId", "region"):
        if not isinstance(doc.get(key), str) or not doc[key]:
            raise ConfigError(f"{key} is required")
    if not re.fullmatch(r"\d{12}", doc["accountId"]):
        raise ConfigError("accountId must be 12 digits")
    if list(doc["endpoints"]) != list(naming.endpoints_for(stage)):
        raise ConfigError(f"endpoints for {stage} must be {list(naming.endpoints_for(stage))}")
    if not isinstance(doc["teamConcurrency"], int) or not 1 <= doc["teamConcurrency"] <= 50:
        raise ConfigError("teamConcurrency must be 1-50")
    if doc["rolloutProfile"] not in PROFILES:
        raise ConfigError(f"rolloutProfile must be one of {PROFILES}")
    sampling = (doc.get("onlineEval") or {}).get("samplingPct")
    if not isinstance(sampling, (int, float)) or not 0 <= sampling <= 100:
        raise ConfigError("onlineEval.samplingPct must be 0-100")
    for arn in doc.get("operatorPrincipals") or []:
        if not _ARN.match(str(arn)):
            raise ConfigError(f"operatorPrincipals entry {arn!r} is not an IAM user or role ARN")
    if doc.get("uiOrigin") is not None and not _ORIGIN.match(str(doc["uiOrigin"])):
        raise ConfigError("uiOrigin must be https://<host>")
    if naming.is_mr(stage) and doc.get("uiPreview") not in {"pending", "wired"}:
        raise ConfigError("uiPreview must be pending or wired for mr-* stacks")
    doc["operatorPrincipals"] = list(doc.get("operatorPrincipals") or [])
    return StageSettings(**{k: doc[k] for k in StageSettings.__dataclass_fields__ if k in doc})


# ---------------------------------------------------------------- release inputs
def check_bootstrap(inputs: ReleaseInputs, applied_exists: bool) -> bool:
    """True for a bootstrap deploy. `bundleArn: null` is allowed only on a stage with no `/applied`."""
    if (inputs.bundleArn is None) != (inputs.bundleVersion is None):
        raise BootstrapNotAllowed("bundleArn and bundleVersion must both be null or both be set")
    if inputs.bundleArn is None and applied_exists:
        raise BootstrapNotAllowed("bundleVersion null is allowed only for a stage with no /applied")
    return inputs.bundleArn is None


def check_bundle_arn(expected: str | None, actual: str | None) -> str | None:
    """BundleArnMismatch when releaseInputs.bundleArn names a different bundle than this stack's."""
    if expected is not None and actual is not None and expected != actual:
        raise BundleArnMismatch(f"releaseInputs.bundleArn {expected} != this stack's bundle {actual}")
    return actual


# ---------------------------------------------------------------- M11_RUNTIME_CONFIG
def runtime_config(*, settings: StageSettings, inputs: ReleaseInputs, tool_arns: Mapping[str, str],
                   guardrail_id: str, guardrail_version: str, artifacts_bucket: str, memory_id: str) -> dict:
    """Runtime-shaping fields only: no manifestVersion or behaviorDigest."""
    return {"stage": settings.stage, "stagekey": settings.stagekey, "bundleArn": inputs.bundleArn,
            "bundleVersion": inputs.bundleVersion, "toolArns": dict(sorted(tool_arns.items())),
            "guardrail": {"id": guardrail_id, "version": str(guardrail_version)}, "flagsKey": "config/flags.json",
            "artifactsBucket": artifacts_bucket,
            "snapshot": {"uri": inputs.snapshot["uri"], "sha256": inputs.snapshot["sha256"]},
            "evidence": {"uri": inputs.evidence["uri"], "sha256": inputs.evidence["sha256"]},
            "memoryId": memory_id, "runtimeDigest": inputs.runtimeDigest}


def serialize_runtime_config(config: Mapping) -> str:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_RUNTIME_CONFIG_CHARS:
        raise RuntimeConfigTooLarge(f"M11_RUNTIME_CONFIG is {len(text)} chars (limit {MAX_RUNTIME_CONFIG_CHARS})")
    return text


# ---------------------------------------------------------------- apply guard (M7)
def apply_guard(stage: str, *, dry_run: bool, env: Mapping[str, str], caller_arn: str) -> None:
    """Staging and prod: a non-preview run needs M11_RELEASE_INPUTS and an m11-<stage>-deployer session."""
    if dry_run or stage not in ("staging", "prod"):
        return
    role = naming.deployer_role(stage)
    if not env.get("M11_RELEASE_INPUTS"):
        raise ApplyRefused(f"use `module11 release deploy --stage {stage}`: M11_RELEASE_INPUTS is not set")
    if f":assumed-role/{role}/" not in caller_arn:
        raise ApplyRefused(f"use `module11 release deploy --stage {stage}`: the caller is not an {role} session")


def adapter_timeout(inputs: ReleaseInputs) -> int:
    """retainedPins.deadlineSeconds + 30."""
    return int((inputs.retainedPins or {}).get("deadlineSeconds") or 150) + 30
