# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The evidence tools' service lookup. Tool handlers only: the runtime does not import it,
so a change here rebuilds the five evidence handlers and not the runtime image.

On unchanged prod the model began calling the evidence tools with `service_name: "checkout"` for checkout-service.
The tool roles may read `evidence/*` but not list it, so S3 answered the missing key with 403, and every tool
returned an opaque `internal ClientError`. Seal now writes `evidence/<hash>/index.json` beside the incident files, so
an unknown service is a recoverable `not_found` that lists the known ones, and a real AWS error keeps its code.

A short or differently cased name that names exactly one known service ("checkout", "Checkout-Service") resolves to
it, and the tool's answer carries the canonical `service`; the not_found retry otherwise adds a call that the
exact-order trajectory rule counts (v26 staging gate, adversarial-tool-content-001). Only the read-only evidence
tools resolve: record_incident_note never imports this module, so a write never aliases.
"""
from __future__ import annotations

import json

from .objects import ObjectNotFound, join, read_incident as _read_incident, verified_get

EVIDENCE_INDEX = "index.json"
_KNOWN: dict[tuple[str, str], list[str] | None] = {}   # per warm environment; the snapshot is hash-pinned


class UnknownService(ObjectNotFound):
    """The evidence snapshot has no file for this service; the message lists the ones it has, so the model can
    retry with a real name (prod saw `service_name: "checkout"` for checkout-service)."""

    def __init__(self, service: str, known: list[str]) -> None:
        self.service, self.known = service, sorted(known)
        super().__init__(f"no evidence for service '{service}'; known services: {', '.join(self.known)}")

    @property
    def public_message(self) -> str:
        return str(self)


def aws_error_code(exc: Exception) -> str | None:
    """A botocore ClientError's code (`AccessDenied`, `403`, ...), else None. Never the message text."""
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) or None


def tool_error_message(exc: Exception) -> str:
    """The `internal` tool error's message: the exception type and, for an AWS error, its code."""
    code = aws_error_code(exc)
    return f"{type(exc).__name__} {code}" if code else type(exc).__name__


def known_services(ref: dict) -> list[str] | None:
    """The services in the evidence snapshot, from its `index.json` (written by Seal beside the incident files),
    or None for a snapshot sealed before the index existed. The tool roles may read `evidence/*` but not list it,
    and S3 then answers a missing key with 403, so the index is how a wrong name is told apart from a denial.
    Read once per snapshot per warm environment: the snapshot is content-addressed and hash-checked."""
    key = (str(ref["uri"]), str(ref.get("sha256")))
    if key in _KNOWN:
        return _KNOWN[key]
    try:
        body = verified_get(join(ref["uri"], EVIDENCE_INDEX), ref.get("sha256"))
    except ObjectNotFound:
        body = None
    except Exception as exc:  # noqa: BLE001 - botocore is not imported offline
        if aws_error_code(exc) not in {"403", "AccessDenied"}:
            raise
        body = None
    services = json.loads(body).get("services") if body is not None else None
    _KNOWN[key] = sorted(str(s) for s in services) if isinstance(services, list) else None
    return _KNOWN[key]


def resolve_service(data: dict, service: str) -> str:
    """The canonical name for `service`: itself when the snapshot knows it (or has no index), the one known service
    it names when exactly one matches case-insensitively as `<name>` or `<name>-service`, else UnknownService
    listing the known services (an unknown or ambiguous name)."""
    ref = (data or {}).get("evidence") or {}
    if not isinstance(service, str) or not ref.get("uri"):
        return service
    known = known_services(ref)
    if known is None or service in known:
        return service
    wanted = service.lower()
    matches = [name for name in known if name.lower() in (wanted, f"{wanted}-service")]
    if len(matches) == 1:
        return matches[0]
    raise UnknownService(service, known)


def read_incident(data: dict, service: str) -> tuple[dict, bytes]:
    """`objects.read_incident` for the canonical name (`resolve_service`). A tool that reports the service calls
    `resolve_service` first, so its answer names the service it actually read."""
    return _read_incident(data, resolve_service(data, service))
