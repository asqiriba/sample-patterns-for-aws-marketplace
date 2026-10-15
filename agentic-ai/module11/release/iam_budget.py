# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""IAM role headroom: how many roles Module 11 creates, and whether the account can hold them. Pure (no AWS).

IAM caps the roles in an account ("Roles per account", Service Quotas code L-FE177D64, default 1,000). Shared
accounts often sit near that ceiling, and a stack that hits it fails halfway through its first `pulumi up` with
`LimitExceeded`. Module 11 creates one role per Lambda (least privilege per tool), so the count is:

    m11-platform                26  m11-image-build, m11-preview-deployer, m11-<stage>-deployer and
                                    m11-<stage>-eval-runner for dev, staging and prod (8); with the
                                    pipeline the 10 CodeBuild project roles, m11-pipeline, the three
                                    GitLab OIDC roles, m11-operator-experiment and the three platform Lambdas (and the
                                    corpus handler)
    m11-harness dev/staging/prod 12 each: runtime, 7 tool Lambdas, adapter, authorizer, control, and the
                                    code-based evaluator's Lambda; plus the operations roles: the
                                    controller, the scheduler's role and the drift checker on every stage, the
                                    shadow worker on dev and prod; the online-evaluation role on
                                    every stage and staging's three A/B Gateway roles (17 on dev and prod, 19
                                    on staging)
    m11-harness mr-<iid>        13 each: the same 12 plus the stack's own eval runner (inside the boundary); no
                                    operations roles

`stack_roles()` derives a stack's names from the harness layout (tests/test_module11_iam_budget.py ties it to the
Pulumi program under mocks). A later phase that adds a role must add it here; the platform test counts the
program's `aws.iam.Role(` call sites so that cannot be missed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import artifacts as A
from . import eval_names, naming

QUOTA_CODE = "L-FE177D64"
QUOTA_NAME = "Roles per account"
DEFAULT_QUOTA = 1000
EDGE_FUNCTIONS = ("adapter", "authorizer", "control")
# Operations roles (infrastructure/harness/operations.py): the shadow worker exists where the stage settings
# have `shadow` (config_model.STAGE_DEFAULTS: dev and prod).
OPERATIONS_ROLES = {"dev": ("controller", "scheduler", "shadow-worker", "drift-check"),
                    "staging": ("controller", "scheduler", "drift-check"),
                    "prod": ("controller", "scheduler", "shadow-worker", "drift-check")}
# The online-evaluation execution role on every non-mr stage, and staging's A/B Gateway roles
# (infrastructure/harness/evaluation.py, gateway.py).
PHASE8_ROLES = {"dev": ("online-eval",), "prod": ("online-eval",),
                "staging": ("online-eval", "ab-gateway", "abtest-driver", "abtest")}
PREVIEW_STACKS = 2          # concurrent mr-<iid> stacks in the demo: the Friday MR and fallbackMr
LOW_MARGIN = 10             # fewer free roles than this after a full deployment is a WARN


def lambda_tools(root: Path) -> list[str]:
    return [t for t in A.tool_names(root) if A.tool_kind(root, t) == "lambda"]


def stack_roles(root: Path, stage: str, *, enable_evaluator: bool = True) -> list[str]:
    """The role names the m11-harness program creates for `stage`."""
    names = [naming.role(stage, "runtime")]
    names += [naming.role(stage, tool) for tool in lambda_tools(Path(root))]
    names += [naming.role(stage, short) for short in EDGE_FUNCTIONS]
    if enable_evaluator:
        names.append(eval_names.evaluator_function(stage))
    if naming.is_mr(stage):
        names.append(naming.eval_runner_role(stage))
    names += [naming.role(stage, short) for short in OPERATIONS_ROLES.get(stage, ())]
    names += [naming.role(stage, short) for short in PHASE8_ROLES.get(stage, ())]
    return sorted(names)


PIPELINE_PROJECT_ROLES = ("m11-analyze", "m11-seal", "m11-deploy", "m11-gate", "m11-bake", "m11-preview",
                          "m11-preview-image", "m11-preview-infra", "m11-bot", "m11-seal-experiment")
PIPELINE_OTHER_ROLES = ("m11-pipeline", "m11-gitlab-ci", "m11-gitlab-ci-infra", "m11-gitlab-promoter",
                        "m11-operator-experiment", "m11-freeze-check", "m11-gitlab-notes", "m11-corpus-handler")


def platform_roles(pipeline: bool = True) -> list[str]:
    """The role names m11-platform creates (infrastructure/platform/__main__.py, oidc.py, events.py): 8 in the
    foundation, 18 more with `enablePipeline=true` (10 CodeBuild project roles, the pipeline, three GitLab OIDC
    roles, m11-operator-experiment and the three Lambdas)."""
    names = ["m11-image-build", naming.deployer_role("mr-0")]
    names += [naming.deployer_role(stage) for stage in naming.STAGES]
    names += [naming.eval_runner_role(stage) for stage in naming.STAGES]
    if pipeline:
        names += [*PIPELINE_PROJECT_ROLES, *PIPELINE_OTHER_ROLES]
    return sorted(names)


@dataclass
class Headroom:
    used: int
    quota: int
    needed: int                          # roles the planned deployment adds (names not yet present)
    planned: int                         # roles the planned deployment holds in total
    breakdown: dict = field(default_factory=dict)
    pending_request: str | None = None   # e.g. "PENDING 1500" when an increase is already requested

    @property
    def free(self) -> int:
        return self.quota - self.used

    @property
    def ok(self) -> bool:
        return self.needed <= self.free

    @property
    def warning(self) -> bool:
        return self.ok and self.free - self.needed < LOW_MARGIN

    def message(self) -> str:
        parts = ", ".join(f"{k} +{v}" for k, v in self.breakdown.items())
        text = (f"IAM {QUOTA_NAME}: {self.used}/{self.quota} used, {self.free} free; the planned deployment holds "
                f"{self.planned} roles, {self.needed} not yet created ({parts})")
        if not self.ok:
            want = self.used + self.needed + LOW_MARGIN
            text += (f". Not enough roles: request an increase of Service Quotas code {QUOTA_CODE} (IAM, us-east-1) to at "
                     f"least {want}: `aws service-quotas request-service-quota-increase --region us-east-1 "
                     f"--service-code iam --quota-code {QUOTA_CODE} --desired-value {want}`, or remove unused roles")
        elif self.warning:
            text += f"; fewer than {LOW_MARGIN} roles would be left (quota {QUOTA_CODE})"
        if self.pending_request:
            text += f" [increase request {self.pending_request}]"
        return text


def plan(root: Path, *, stages: Sequence[str] = naming.STAGES, preview_stacks: int = PREVIEW_STACKS,
         include_platform: bool = True) -> dict[str, list[str]]:
    """Named roles per stack for a full deployment; `mr-*` stacks are planned anonymously as `preview-<n>`."""
    out: dict[str, list[str]] = {}
    if include_platform:
        out["platform"] = platform_roles()
    for stage in stages:
        out[stage] = stack_roles(root, stage)
    per_mr = len(stack_roles(root, "mr-0"))
    for n in range(preview_stacks):
        out[f"preview-{n + 1}"] = [f"(mr-<iid> role {i + 1})" for i in range(per_mr)]
    return out


def headroom(planned: dict[str, Iterable[str]], *, used: int, quota: int, existing: Iterable[str],
             pending_request: str | None = None) -> Headroom:
    """Compare the planned roles with the account. Named roles that already exist need no new slot; anonymous
    preview stacks are matched against existing `m11-mr-*` roles, so live preview stacks count as present."""
    present = set(existing)
    named = {stack: list(names) for stack, names in planned.items() if not stack.startswith("preview-")}
    previews = [list(names) for stack, names in planned.items() if stack.startswith("preview-")]
    claimed = {n for names in named.values() for n in names}
    existing_mr = sum(1 for name in present if name.startswith("m11-mr-") and name not in claimed)
    breakdown: dict[str, int] = {}
    for stack, names in named.items():
        breakdown[stack] = sum(1 for n in names if n not in present)
    if previews:
        slots = sum(len(names) for names in previews)
        breakdown[f"{len(previews)} mr-<iid> stacks"] = max(0, slots - existing_mr)
    total = sum(len(v) for v in named.values()) + sum(len(v) for v in previews)
    return Headroom(used=int(used), quota=int(quota), needed=sum(breakdown.values()), planned=total,
                    breakdown=breakdown, pending_request=pending_request)
