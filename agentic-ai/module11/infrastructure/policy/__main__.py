# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CrossGuard pack `m11-guardrails`: P1-P11, all mandatory.

Run on every preview and up of m11-harness and m11-platform:
    pulumi preview --stack <s> --policy-pack module11/infrastructure/policy
Inside `pulumi up` the pack runs in the preview phase, and a mandatory violation stops the update
before any resource changes. The rules are in rules.py; pack.py maps resource properties.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pulumi_policy import (  # noqa: E402
    EnforcementLevel, PolicyPack, ResourceValidationPolicy, StackValidationPolicy,
)
from pulumi_policy.proxy import UnknownValueError  # noqa: E402

from module11.infrastructure.policy import pack  # noqa: E402


class _Unknown:
    """A value Pulumi cannot know during preview (it is checked again when `up` registers it)."""

    def __repr__(self) -> str:
        return "<unknown>"


UNKNOWN = _Unknown()


def plain(value):
    """Copy a CrossGuard property proxy into plain dicts and lists, unknowns as UNKNOWN."""
    if isinstance(value, Mapping):
        out = {}
        for key in value:
            try:
                out[key] = plain(value[key])
            except UnknownValueError:
                out[key] = UNKNOWN
        return out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = []
        for index in range(len(value)):
            try:
                items.append(plain(value[index]))
            except UnknownValueError:
                items.append(UNKNOWN)
        return items
    return value


def _operators() -> list[str]:
    raw = os.environ.get("M11_OPERATOR_PRINCIPALS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def validate_resource(args, report_violation) -> None:
    for message in pack.check_resource(args.resource_type, plain(args.props), operators=_operators()):
        report_violation(message, None)


def validate_stack(args, report_violation) -> None:
    resources = [(r.resource_type, plain(r.props)) for r in args.resources]
    for message in pack.check_stack(resources, pack.retained_from_env()):
        report_violation(message, None)


PolicyPack(
    name="m11-guardrails",
    enforcement_level=EnforcementLevel.MANDATORY,
    policies=[
        ResourceValidationPolicy(
            name="m11-resource-rules",
            description="P1 IAM statements, P2 publish, P3 tags, P4 env values, P5 log retention, "
                        "P6 SourceAccount, P7 public network, P8 mr-* boundary, operator trust",
            validate=validate_resource),
        StackValidationPolicy(
            name="m11-stack-rules",
            description="P9 runtime invoke list, P10 CodeBuild caches, P11 who may start which build",
            validate=validate_stack),
    ],
)
