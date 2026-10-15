# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock guardrail on input and output.

`ApplyGuardrail` with the pinned `guardrailIdentifier` and `guardrailVersion` from
`M11_RUNTIME_CONFIG`: `source=INPUT` on the change summary before the loop, `source=OUTPUT` on
the final review text and on any planner draft. On `GUARDRAIL_INTERVENED` the invocation
result is `status=blocked` with the guardrail's configured message. The check fails closed: if
the guardrail cannot be reached, the invocation fails instead of returning unchecked text.
"""
from __future__ import annotations

from dataclasses import dataclass

INTERVENED = "GUARDRAIL_INTERVENED"
LOCAL_ID = "local-passthrough"


class GuardrailUnavailable(RuntimeError):
    """ApplyGuardrail failed; the runtime fails the invocation (fail closed)."""


class GuardrailBlocked(RuntimeError):
    """The guardrail intervened; `message` is the configured blocked message."""

    def __init__(self, source: str, message: str) -> None:
        super().__init__(f"guardrail intervened on {source}")
        self.source = source
        self.message = message


@dataclass(frozen=True)
class GuardrailResult:
    action: str          # NONE | GUARDRAIL_INTERVENED
    text: str            # the configured message when intervened, else the input text

    @property
    def intervened(self) -> bool:
        return self.action == INTERVENED


class BedrockGuardrail:
    def __init__(self, identifier: str, version: str, client=None) -> None:
        self.identifier = identifier
        self.version = version
        self._client = client

    def _runtime(self):
        if self._client is None:
            from module11.aws import client
            self._client = client("bedrock-runtime")
        return self._client

    def apply(self, text: str, source: str) -> GuardrailResult:
        if source not in {"INPUT", "OUTPUT"}:
            raise ValueError("source must be INPUT or OUTPUT")
        try:
            response = self._runtime().apply_guardrail(
                guardrailIdentifier=self.identifier, guardrailVersion=self.version, source=source,
                content=[{"text": {"text": text}}])
        except Exception as exc:
            raise GuardrailUnavailable(type(exc).__name__) from exc
        action = response.get("action", "NONE")
        if action == INTERVENED:
            outputs = response.get("outputs") or [{}]
            return GuardrailResult(INTERVENED, outputs[0].get("text") or "Blocked by guardrail.")
        return GuardrailResult(action, text)


class LocalGuardrail:
    """Pass-through used only by the loopback local run with the fake model (`--fake-model`)."""
    identifier = LOCAL_ID
    version = "local"

    def apply(self, text: str, source: str) -> GuardrailResult:
        return GuardrailResult("NONE", text)


def from_config(guardrail: dict, *, local: bool = False):
    if guardrail["id"] == LOCAL_ID:
        if not local:
            raise ValueError(f"guardrail id {LOCAL_ID} is accepted only in a local run")
        return LocalGuardrail()
    return BedrockGuardrail(guardrail["id"], guardrail["version"])


def check(guardrail, text: str, source: str) -> str:
    """Apply and raise GuardrailBlocked on intervention; returns the checked text."""
    result = guardrail.apply(text, source)
    if result.intervened:
        raise GuardrailBlocked(source, result.text)
    return text
