# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""External tool surfaces pinned by hash. Pure.

`surface_hash()` is sha256 over the canonical `targetConfiguration` and `metadataConfiguration`
of a GetGatewayTarget response with ARNs and IDs masked to `<arn>`, so a redeploy that only
changes generated identifiers is not a change. For an MCP target it hashes the sorted tool
definitions {name, description, inputSchema} the model would see. Analyze fails with
`ExternalSurfaceChanged` on a mismatch; drift `--external` reports it as a warning; offline
commands hash only the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .hashing import json_hash

MASK = "<arn>"
_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{0,12}:[^\s\"']+")
_ID_KEY = re.compile(r"(?i)(arn|id)$")


class ExternalSurfaceChanged(RuntimeError):
    def __init__(self, surface_id: str, pinned: str | None, observed: str) -> None:
        super().__init__(f"external surface {surface_id} changed: pinned {pinned}, observed {observed}; "
                         "re-pin it in agent/external_surfaces.yaml (full suite, trajectory-weighted)")
        self.surface_id, self.pinned, self.observed = surface_id, pinned, observed


def mask(value: Any, key: str = "") -> Any:
    """Replace ARNs anywhere, and the values of *Arn / *Id keys, with `<arn>`."""
    if isinstance(value, Mapping):
        return {k: mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v, key) for v in value]
    if isinstance(value, str):
        if key and _ID_KEY.search(key):
            return MASK
        return _ARN.sub(MASK, value)
    return value


def tool_definitions(tools: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The model-visible part of each tool, sorted by name."""
    return sorted(({"name": t["name"], "description": t.get("description", ""), "inputSchema": t.get("inputSchema", {})}
                   for t in tools), key=lambda t: t["name"])


def _inline_tools(response: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    mcp = (response.get("targetConfiguration") or {}).get("mcp") or {}
    for kind in mcp.values():
        payload = ((kind or {}).get("toolSchema") or {}).get("inlinePayload") if isinstance(kind, Mapping) else None
        if payload:
            return payload
    return None


def surface_hash(response: Mapping[str, Any], tools: Sequence[Mapping[str, Any]] | None = None) -> str:
    """Hash of a GetGatewayTarget response, or of an MCP target's listed tools when given."""
    listed = tools if tools is not None else (_inline_tools(response) if response.get("kind") == "mcp" else None)
    if listed is not None:
        return json_hash(tool_definitions(listed))
    return json_hash(mask({"targetConfiguration": response.get("targetConfiguration") or {},
                           "metadataConfiguration": response.get("metadataConfiguration") or {}}))


@dataclass(frozen=True)
class Mismatch:
    id: str
    pinned: str | None
    observed: str


def compare(surfaces: Iterable[Mapping[str, Any]], observed: Mapping[str, str]) -> list[Mismatch]:
    """Pinned surfaces whose live hash differs (surfaces not observed are skipped)."""
    return [Mismatch(s["id"], s.get("sha256"), observed[s["id"]]) for s in surfaces
            if s["id"] in observed and s.get("sha256") != observed[s["id"]]]


def enforce(surfaces: Iterable[Mapping[str, Any]], observed: Mapping[str, str]) -> None:
    """Analyze (build): fail on the first changed surface."""
    for m in compare(surfaces, observed):
        raise ExternalSurfaceChanged(m.id, m.pinned, m.observed)
