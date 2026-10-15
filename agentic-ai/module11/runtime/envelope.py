# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The internal invocation envelope.

    {"identity": {team, principal},
     "request": {change_summary, request_id},
     "context": {conversationId?, variant, shadow?, scenarioId?, rolloutId?, traceparent?,
                 manifestVersion, behaviorDigest, release}}

Only trusted IAM callers build it (the m11 adapter, shadow worker, eval runner and the staging
Gateway); the public API never accepts it from a client. Unknown keys are rejected,
`request` goes through Module 10's `TeamReviewRequest.from_http`, and `shadow` is accepted only
with `variant` shadow or eval, so a user session can never suppress side effects. Release
identity (manifestVersion, behaviorDigest, release) is required: the runtime stamps it on every
span, result, side effect and memory record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from module10.reviewer import TeamReviewRequest

TOP_KEYS = frozenset({"identity", "request", "context"})
CONTEXT_KEYS = frozenset({"conversationId", "variant", "shadow", "scenarioId", "rolloutId", "traceparent",
                          "manifestVersion", "behaviorDigest", "release"})
VARIANTS = frozenset({"stable", "candidate", "preview", "staging", "dev", "eval", "shadow"})
SHADOW_VARIANTS = frozenset({"shadow", "eval"})

IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MANIFEST_VERSION = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ/[0-9a-f]{7}$")
BEHAVIOR_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE = re.compile(r"^[a-z0-9-]{1,40}$")
SCENARIO_ID = re.compile(r"^[a-z0-9-]{3,64}$")
ROLLOUT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")


class EnvelopeError(ValueError):
    """The envelope is not acceptable; the HTTP layer answers 400 `Invalid invocation`."""


@dataclass(frozen=True)
class Invocation:
    team: str
    principal: str
    change_summary: str
    request_id: str
    conversation_id: str
    variant: str
    shadow: bool
    manifest_version: str
    behavior_digest: str
    release: str
    scenario_id: str | None = None
    rollout_id: str | None = None
    traceparent: str | None = None

    def identity(self) -> dict[str, str]:
        """The release identity the edge stamped from the control item."""
        return {"manifestVersion": self.manifest_version, "behaviorDigest": self.behavior_digest,
                "release": self.release}


def _match(pattern: re.Pattern, value: Any, name: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise EnvelopeError(f"{name} is invalid")
    return value


def parse(payload: Any) -> Invocation:
    """Validate the whole envelope or raise EnvelopeError."""
    if not isinstance(payload, dict) or set(payload) - TOP_KEYS or not {"identity", "request"} <= set(payload):
        raise EnvelopeError("envelope must have identity and request, and only identity, request, context")
    identity = payload["identity"]
    if not isinstance(identity, dict) or set(identity) != {"team", "principal"}:
        raise EnvelopeError("identity must be exactly {team, principal}")
    team = _match(IDENTIFIER, identity["team"], "identity.team")
    principal = _match(IDENTIFIER, identity["principal"], "identity.principal")
    request = payload["request"]
    if not isinstance(request, dict):
        raise EnvelopeError("request must be an object")
    try:
        review = TeamReviewRequest.from_http(request, team, principal)
    except (ValueError, TypeError, AttributeError) as exc:
        raise EnvelopeError(f"request: {exc}") from exc
    _match(IDENTIFIER, review.request_id, "request.request_id")
    context = payload.get("context")
    if not isinstance(context, dict):
        raise EnvelopeError("context is required (release identity and variant)")
    unknown = set(context) - CONTEXT_KEYS
    if unknown:
        raise EnvelopeError(f"context has unknown keys: {sorted(unknown)}")
    variant = context.get("variant")
    if variant not in VARIANTS:
        raise EnvelopeError("context.variant is invalid")
    shadow = context.get("shadow", False)
    if not isinstance(shadow, bool):
        raise EnvelopeError("context.shadow must be a boolean")
    if shadow and variant not in SHADOW_VARIANTS:
        raise EnvelopeError("context.shadow is accepted only with variant shadow or eval")
    conversation = context.get("conversationId", review.request_id)
    if not isinstance(conversation, str) or not (CONVERSATION_ID.match(conversation) or conversation == review.request_id):
        raise EnvelopeError("context.conversationId is invalid")
    optional = {}
    for key, pattern in (("scenarioId", SCENARIO_ID), ("rolloutId", ROLLOUT_ID), ("traceparent", TRACEPARENT)):
        if context.get(key) is not None:
            optional[key] = _match(pattern, context[key], f"context.{key}")
    return Invocation(
        team=team, principal=principal, change_summary=review.change_summary, request_id=review.request_id,
        conversation_id=conversation, variant=variant, shadow=shadow,
        manifest_version=_match(MANIFEST_VERSION, context.get("manifestVersion"), "context.manifestVersion"),
        behavior_digest=_match(BEHAVIOR_DIGEST, context.get("behaviorDigest"), "context.behaviorDigest"),
        release=_match(RELEASE, context.get("release"), "context.release"),
        scenario_id=optional.get("scenarioId"), rollout_id=optional.get("rolloutId"),
        traceparent=optional.get("traceparent"))
