# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Drift checker. Pure: `check()` compares the deployed manifests, the release
ledger and live observations gathered by lambdas/drift_check (or `module11 drift`) and returns
a DriftReport. There is no auto-revert; `revert_endpoint()` is the endpoint half of
`module11 drift revert`, over injected calls.

Observations (all optional; a missing component is not checked):
  endpoints        {qualifier: GetAgentRuntimeEndpoint fields (liveVersion, targetVersion, status)}
  runtimeVersions  {version: GetAgentRuntime(version) response}
  bundle           {"pinnedComponentSha256": sha, "versions": [versionId, ...]}
  outcomes         adapter outcome items of the last 15 minutes (with startedAt epoch seconds)
  manifests        {manifestVersion: manifest} the outcome items name
  tools            {tool: {"codeSha256": ...}};  runtimeToolArns {tool: qualified ARN}
  policies         {role: policy document (string, possibly URL-encoded, or dict)}
  guardrail        GetGuardrail(id, version) response
  flagsSha256      normalized hash of config/flags.json;  snapshots {corpus: sha256}
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import guardrail as guardrail_projection
from .hashing import json_hash
from .manifest import runtime_ids

ACTIVE = ("SHADOWING", "WARMING", "OBSERVING", "ADVANCING", "ROLLING_BACK")
STAMPING_GRACE_SECONDS = 60
BUNDLE_LEDGER_KINDS = ("bundle-version", "initial")


@dataclass
class Finding:
    component: str
    field: str
    declared: Any
    observed: Any
    status: str | None = None
    severity: str = "drift"

    def text(self) -> str:
        status = f" ({self.status})" if self.status else ""
        return f"{self.component}.{self.field}: {self.observed}{status} != declared {self.declared}"


@dataclass
class DriftReport:
    stage: str
    checkedAt: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def drift_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "drift")

    def to_dict(self) -> dict:
        return {"stage": self.stage, "checkedAt": self.checkedAt, "findings": [asdict(f) for f in self.findings]}


def policy_digest(document: str | Mapping[str, Any]) -> str:
    """sha256 of the canonical policy document: URL-decoded, keys sorted, statements sorted."""
    doc = json.loads(urllib.parse.unquote(document)) if isinstance(document, str) else dict(document)
    stmts = doc.get("Statement")
    stmts = [stmts] if isinstance(stmts, Mapping) else list(stmts or [])
    norm = []
    for s in stmts:
        s = dict(s)
        for key in ("Action", "NotAction", "Resource", "NotResource"):
            if key in s:
                s[key] = sorted([s[key]] if isinstance(s[key], str) else s[key])
        norm.append(s)
    doc["Statement"] = sorted(norm, key=lambda s: json.dumps(s, sort_keys=True))
    return json_hash(doc)


def ledger_versions(entries: Iterable[tuple[str, Mapping[str, Any]]]) -> set[str]:
    """Bundle versions the ledger records, from a key-ordered prefix listing."""
    return {str(e["versionId"]) for _, e in sorted(entries, key=lambda kv: kv[0])
            if e.get("kind") in BUNDLE_LEDGER_KINDS and e.get("versionId")}


def _deployment(manifest: Mapping | None, stage: str) -> dict:
    return dict(((manifest or {}).get("deployment") or {}).get(stage) or {})


def check(stage: str, current: Mapping[str, Any], candidate: Mapping[str, Any] | None, control: Mapping | None,
          observed: Mapping[str, Any], ledger: Iterable[tuple[str, Mapping[str, Any]]] = (),
          applied: Mapping[str, Any] | None = None, pins: Mapping[str, Any] | None = None,
          checked_at: str = "") -> DriftReport:
    report = DriftReport(stage, checked_at)
    add = report.findings.append
    in_rollout = bool(control and control.get("state") in ACTIVE)
    cand_manifest = candidate if in_rollout and candidate and candidate.get("state") != "none" else current
    # Every declared endpoint: stable and candidate on rollout stages, and staging's single `staging` endpoint, which
    # always follows /current (a staging-only hotfix went unreported when only stable/candidate were read).
    declared_all = _deployment(current, stage).get("endpoints") or {}
    pinned_by = {"stable": current, "candidate": cand_manifest,
                 **{q: current for q in declared_all if q not in ("stable", "candidate")}}
    declared_endpoints = {q: _deployment(m, stage).get("endpoints", {}).get(q) for q, m in pinned_by.items()}

    # Endpoints.
    for qualifier, obs in (observed.get("endpoints") or {}).items():
        declared = declared_endpoints.get(qualifier)
        if declared is None:
            continue
        target, live = obs.get("targetVersion"), obs.get("liveVersion")
        if target is not None and int(target) != int(declared):
            add(Finding("endpoint", qualifier, int(declared), f"target {target}", obs.get("status")))
        elif live is None or int(live) != int(declared):
            add(Finding("endpoint", qualifier, int(declared), f"live {live}", obs.get("status")))

    # Runtime versions: the full canonical configuration.
    checked: set[str] = set()
    for qualifier, manifest in pinned_by.items():
        version = declared_endpoints.get(qualifier)
        config = (observed.get("runtimeVersions") or {}).get(str(version)) if version is not None else None
        if config is None or str(version) in checked:
            continue
        checked.add(str(version))
        want = _deployment(manifest, stage).get("runtimeConfigSha256")
        got = runtime_ids(config, dict(manifest), stage)[0]
        if want and got != want:
            add(Finding("runtime", f"v{version}.runtimeConfigSha256", want, got))

    # Bundle.
    bundle = observed.get("bundle")
    if bundle:
        want = ((current.get("build") or {}).get("bundleComponent") or {}).get("sha256")
        if bundle.get("pinnedComponentSha256") and bundle["pinnedComponentSha256"] != want:
            add(Finding("bundle", "componentSha256", want, bundle["pinnedComponentSha256"]))
        known = ledger_versions(ledger)
        for version in bundle.get("versions") or []:
            if str(version) not in known:
                add(Finding("bundle", "versions", "a ledgered version", str(version), "unexpected version"))

    # Release stamping: adapter items only.
    manifests = dict(observed.get("manifests") or {})
    last_endpoints = float((control or {}).get("endpointsWrittenAt") or 0)
    for item in observed.get("outcomes") or []:
        if item.get("shadow"):
            continue
        if float(item.get("startedAt", 0)) < last_endpoints + STAMPING_GRACE_SECONDS:
            continue
        mv = item.get("manifestVersion")
        manifest = manifests.get(mv)
        if manifest is None:
            add(Finding("stamping", str(item.get("requestId")), mv, "manifest not found", severity="warning"))
            continue
        want = (manifest.get("digests") or {}).get("runtime")
        if item.get("runtimeDigest") != want:
            add(Finding("stamping", str(item.get("requestId")), want, item.get("runtimeDigest")))

    # Tools.
    handlers = (current.get("build") or {}).get("handlers") or {}
    for tool, obs in (observed.get("tools") or {}).items():
        want = (handlers.get(tool) or {}).get("codeSha256")
        if want and obs.get("codeSha256") != want:
            add(Finding("tool", f"{tool}.codeSha256", want, obs.get("codeSha256")))
    runtime_arns = observed.get("runtimeToolArns")
    own_arns = ((pins or {}).get("own") or {}).get("toolArns")
    if runtime_arns is not None and own_arns is not None and dict(runtime_arns) != dict(own_arns):
        add(Finding("tool", "toolArns", dict(own_arns), dict(runtime_arns)))

    # IAM (M7).
    digests = (applied or {}).get("policyDigests") or {}
    for role, document in (observed.get("policies") or {}).items():
        want = digests.get(role)
        got = policy_digest(document)
        if want and got != want:
            add(Finding("iam", role, want, got))

    # Guardrail.
    if observed.get("guardrail") is not None:
        want = (current.get("guardrail") or {}).get("definitionSha256")
        got = guardrail_projection.response_sha256(observed["guardrail"])
        if want and got != want:
            add(Finding("guardrail", "definitionSha256", want, got))

    # Flags and snapshots.
    if observed.get("flagsSha256") is not None:
        want = (current.get("flags") or {}).get("sha256")
        if observed["flagsSha256"] != want:
            add(Finding("flags", "sha256", want, observed["flagsSha256"]))
    snapshots = observed.get("snapshots") or {}
    for corpus in current.get("retrieval") or []:
        got = snapshots.get(corpus.get("corpus"))
        if got is not None and got != corpus.get("snapshotSha256"):
            add(Finding("snapshot", str(corpus.get("corpus")), corpus.get("snapshotSha256"), got))
    return report


def external_warnings(model_lifecycles: Mapping[str, str], builtin_fingerprint: tuple[str | None, str] | None,
                      surfaces: Sequence[tuple[str, str | None, str]]) -> list[Finding]:
    """`--external` probes: warnings only, never drift."""
    out = [Finding("model", model, "ACTIVE", status, severity="warning")
           for model, status in sorted(model_lifecycles.items()) if status == "LEGACY"]
    if builtin_fingerprint and builtin_fingerprint[0] and builtin_fingerprint[0] != builtin_fingerprint[1]:
        out.append(Finding("evaluators", "builtinFingerprint", builtin_fingerprint[0], builtin_fingerprint[1],
                           severity="warning"))
    out += [Finding("external", sid, pinned, got, severity="warning") for sid, pinned, got in surfaces if pinned != got]
    return out


class ConflictException(Exception):
    pass


def revert_endpoint(get_endpoint: Callable[[], Mapping[str, Any]], update_endpoint: Callable[[int], None],
                    version: int, sleep: Callable[[float], None] = time.sleep, wait_seconds: float = 600,
                    interval: float = 5.0) -> Mapping[str, Any]:
    """`drift revert` for one endpoint (N5): wait out UPDATING, retarget (ConflictException retried
    3 times 5 s apart), then poll until READY and live == version (the 4.2.16 rule)."""
    waited = 0.0
    while get_endpoint().get("status") == "UPDATING":
        if waited >= wait_seconds:
            raise TimeoutError("endpoint still UPDATING; a hotfix may be in flight")
        sleep(interval)
        waited += interval
    for attempt in range(4):
        try:
            update_endpoint(int(version))
            break
        except ConflictException:
            if attempt == 3:
                raise
            sleep(interval)
    waited = 0.0
    while True:
        obs = get_endpoint()
        target = obs.get("targetVersion")
        if obs.get("status") == "READY" and int(obs.get("liveVersion") or -1) == int(version) and \
                (target is None or int(target) == int(version)):
            return obs
        if waited >= wait_seconds:
            raise TimeoutError(f"endpoint not READY on v{version}")
        sleep(interval)
        waited += interval
