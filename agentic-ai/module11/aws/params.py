# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SSM parameters of a stage and of the account.

Per stage: `/m11/devops-companion/<stage>/{current,candidate,applied,drift}`. Pulumi creates them
with the placeholder `{"state": "none"}` (and `ignore_changes=["value"]`), so a stage with no
release yet reads as absent here. Account level: `/freeze` and `/latch/<behaviorDigest hex>`.
`/current` and `/candidate` are Advanced (8 KB); `/applied` and `/drift` are Standard.
"""
from __future__ import annotations

import json
from typing import Any

from module11.release import naming

NONE = {"state": "none"}
ADVANCED = {"current", "candidate"}


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def get_json(ssm: Any, name: str) -> dict | None:
    """The parameter's JSON value, or None when it does not exist or holds the placeholder."""
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:
        if _code(exc) == "ParameterNotFound":
            return None
        raise
    doc = json.loads(value)
    return None if doc == NONE else doc


def put_json(ssm: Any, name: str, doc: dict, *, advanced: bool = False) -> int:
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    response = ssm.put_parameter(Name=name, Value=body, Type="String", Overwrite=True,
                                 Tier="Advanced" if advanced else "Standard")
    return int(response.get("Version", 0))


def stage_get(ssm: Any, stage: str, leaf: str) -> dict | None:
    return get_json(ssm, naming.param(stage, leaf))


def stage_put(ssm: Any, stage: str, leaf: str, doc: dict) -> int:
    return put_json(ssm, naming.param(stage, leaf), doc, advanced=leaf in ADVANCED)


def freeze(ssm: Any) -> dict:
    return get_json(ssm, naming.freeze_param()) or {"state": "none"}


def latched(ssm: Any, behavior_digest: str) -> dict | None:
    """The latch record when the digest is latched. `latch clear` keeps the parameter with
    `state: cleared` (its audit trail; the controller role cannot delete it), which is not a latch."""
    doc = get_json(ssm, naming.latch_param(behavior_digest))
    return doc if doc and doc.get("state") == "latched" else None


def shared_outputs(ssm: Any) -> dict:
    doc = get_json(ssm, naming.SHARED_OUTPUTS)
    if not doc:
        raise RuntimeError(f"{naming.SHARED_OUTPUTS} is missing: apply m11-platform first")
    return doc
