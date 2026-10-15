# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Names and identifier patterns shared by the engines, the CLI and the programs."""
from __future__ import annotations

import re

from .hashing import hex_of

HARNESS = "devops-companion"
PARAM_ROOT = f"/m11/{HARNESS}"
ROLLOUT_STAGES = ("prod", "dev")
SINGLE_STAGES = ("staging",)
STAGES = ("dev", "staging", "prod")

MANIFEST_VERSION = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ/[0-9a-f]{7}$")
BEHAVIOR_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_SLUG = re.compile(r"^[a-z0-9-]{1,40}$")
RELEASE_TAG = re.compile(r"^harness/v(\d+)$")
MR_STAGE = re.compile(r"^mr-(\d{1,9})$")
SSM_NAME = re.compile(r"^[a-zA-Z0-9_.\-/]+$")
SCENARIO_ID = re.compile(r"^[a-z0-9-]{3,64}$")


class NameError_(ValueError):
    pass


def check_stage(stage: str) -> str:
    if stage in STAGES or MR_STAGE.match(stage):
        return stage
    raise NameError_(f"unknown stage {stage!r}: expected one of {', '.join(STAGES)} or mr-<n>")


def is_rollout_stage(stage: str) -> bool:
    return stage in ROLLOUT_STAGES


def param(stage: str, leaf: str) -> str:
    """SSM parameter per stage: current | candidate | applied | drift."""
    if leaf not in {"current", "candidate", "applied", "drift"}:
        raise NameError_(f"unknown stage parameter {leaf!r}")
    return _ssm(f"{PARAM_ROOT}/{check_stage(stage)}/{leaf}")


def freeze_param() -> str:
    return f"{PARAM_ROOT}/freeze"


def latch_param(behavior_digest: str) -> str:
    """`/m11/devops-companion/latch/<hex>`: the hex without `sha256:` because `:` is invalid."""
    if not BEHAVIOR_DIGEST.match(behavior_digest):
        raise NameError_("behaviorDigest must be sha256:<64 hex>")
    return _ssm(f"{PARAM_ROOT}/latch/{hex_of(behavior_digest)}")


def _ssm(name: str) -> str:
    if not SSM_NAME.match(name):
        raise NameError_(f"invalid SSM parameter name {name!r}")
    return name


def release_slug(tag: str | None, mr_iid: int | None = None) -> str:
    """`harness/v8` -> `harness-v8`; an MR preview -> `mr-<iid>`."""
    if mr_iid is not None:
        return f"mr-{int(mr_iid)}"
    if not tag or not RELEASE_TAG.match(tag):
        raise NameError_(f"release tag must match harness/v<n>, got {tag!r}")
    return tag.replace("/", "-")


def manifest_version(timestamp_utc: str, commit: str) -> str:
    value = f"{timestamp_utc}/{commit[:7]}"
    if not MANIFEST_VERSION.match(value):
        raise NameError_(f"invalid manifestVersion {value!r}")
    return value


def tool_function(stage: str, tool: str) -> str:
    return f"m11-{check_stage(stage)}-{tool}"


# ---------------------------------------------------------------- per-stage resource names
PROJECT_TAG = "m11-devops-companion"
STATE_BUCKET = "m11-pulumi-state-{account}-{region}"
PREVIEW_STATE_BUCKET = "m11-pulumi-state-preview-{account}-{region}"
RELEASE_BUCKET = "m11-release-{account}-{region}"
PIPELINE_BUCKET = "m11-pipeline-artifacts-{account}-{region}"
SECRETS_ALIAS = "alias/m11-pulumi-secrets"
PREVIEW_SECRETS_ALIAS = "alias/m11-pulumi-secrets-preview"
IMAGE_REPOSITORY = "m11/devops-companion"
PREVIEW_IMAGE_REPOSITORY = "m11/devops-companion-preview"
SHARED_OUTPUTS = f"{PARAM_ROOT}/shared/outputs"
BOUNDARY = "m11-mr-boundary"


def stagekey(stage: str) -> str:
    """`dev`, `staging`, `prod`, or `mr<iid>` (runtime names allow no hyphen)."""
    match = MR_STAGE.match(check_stage(stage))
    return f"mr{match.group(1)}" if match else stage


def is_mr(stage: str) -> bool:
    return bool(MR_STAGE.match(stage))


def runtime_name(stage: str) -> str:
    return f"m11_{stagekey(stage)}_companion"


def bundle_name(stage: str) -> str:
    return f"m11_{stagekey(stage)}_companion_cfg"


def memory_name(stage: str) -> str:
    return f"m11_{stagekey(stage)}_memory"


def function(stage: str, short: str) -> str:
    """Stage-owned Lambda functions: tools, `adapter`, `control`, `authorizer`, `eval-…`."""
    return f"m11-{check_stage(stage)}-{short}"


def role(stage: str, short: str) -> str:
    name = f"m11-{check_stage(stage)}-{short}"
    if len(name) > 64:
        raise NameError_(f"role name {name!r} exceeds 64 characters")
    return name


def table(stage: str, short: str) -> str:
    return f"m11-{check_stage(stage)}-{short}"


def artifacts_bucket(stage: str, account: str, region: str) -> str:
    return f"m11-{stagekey(stage)}-artifacts-{account}-{region}"


def endpoints_for(stage: str) -> tuple[str, ...]:
    """prod and dev: stable + candidate; staging: staging; mr-*: preview."""
    if is_rollout_stage(stage):
        return ("stable", "candidate")
    return ("preview",) if is_mr(check_stage(stage)) else (stage,)


def deployer_role(stage: str) -> str:
    return "m11-preview-deployer" if is_mr(stage) else f"m11-{check_stage(stage)}-deployer"


def eval_runner_role(stage: str) -> str:
    return f"m11-{check_stage(stage)}-eval-runner"
