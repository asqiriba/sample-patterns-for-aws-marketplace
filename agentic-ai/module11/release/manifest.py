# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The release manifest, the bundle component and runtime identity.

Pure functions only. The declared manifest is built from a checkout; deployed manifests add a
`deployment.<stage>` section written by `release deploy`.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable


from . import artifacts as A
from . import closure as closures
from . import stage as cfg
from .hashing import json_hash, sha256_bytes
from .naming import manifest_version
from .normalize import canonical_json, normalize

SCHEMA = "m11.manifest/2"
BUNDLE_SCHEMA = "m11.bundle/2"
MAX_COMPACT_BYTES = 7500
SCHEMA_PATH = Path(__file__).with_name("manifest.schema.json")

# Caller-settable runtime-version configuration: GetAgentRuntime response members that
# UpdateAgentRuntime also accepts, minus agentRuntimeId and platformVersion. A unit test
# compares this with the installed botocore model.
RUNTIME_CONFIG_MEMBERS = (
    "agentRuntimeArtifact", "authorizerConfiguration", "capacityProviderConfiguration", "description",
    "environmentVariables", "filesystemConfigurations", "lifecycleConfiguration", "metadataConfiguration",
    "networkConfiguration", "protocolConfiguration", "requestHeaderConfiguration", "roleArn",
)
RUNTIME_CONFIG_ENV = "M11_RUNTIME_CONFIG"


class ManifestError(ValueError):
    pass


class ManifestTooLarge(ManifestError):
    pass


class ReleaseInputsCorrupt(ManifestError):
    pass


# ---------------------------------------------------------------- bundle component
def bundle_component(repo_root: str | Path) -> dict:
    """The configuration bundle component: exact normalized source documents, no identity."""
    root = Path(repo_root)
    prompts = {agent: normalize(f"{agent}.md", (root / A.AGENT / "prompts" / f"{agent}.md").read_bytes()).decode("utf-8")
               for agent in A.agents(root)}
    tools = {}
    for tool in A.tool_names(root):
        base = f"{A.TOOLS}/{tool}"
        tools[tool] = {
            "meta": cfg.read(root, f"{base}/tool.json"),
            "schema": cfg.read(root, f"{base}/schema.json"),
            "description": normalize("d.md", (root / base / "description.md").read_bytes()).decode("utf-8"),
        }
    return {"schema": BUNDLE_SCHEMA, "prompts": prompts, "tools": tools,
            "models": cfg.read(root, A.SINGLE["model"]), "orchestration": cfg.read(root, A.SINGLE["orchestration"])}


def component_bytes(component: dict) -> bytes:
    return canonical_json(component)


def _embedded_hashes(component: dict) -> dict[str, str]:
    out = {f"instructions/{a}": sha256_bytes(t.encode("utf-8")) for a, t in component.get("prompts", {}).items()}
    for tool, docs in component.get("tools", {}).items():
        out[f"tool/{tool}/meta"] = json_hash(docs.get("meta"))
        out[f"tool/{tool}/schema"] = json_hash(docs.get("schema"))
        out[f"tool/{tool}/description"] = sha256_bytes(normalize("d.md", docs.get("description", "").encode("utf-8")))
    out["model"] = json_hash(component.get("models"))
    out["orchestration"] = json_hash(component.get("orchestration"))
    return out


def verify_component(manifest: dict, data: bytes) -> dict:
    """Check the sealed object's sha256, then every embedded document against the manifest's
    artifact hashes. Raises ReleaseInputsCorrupt("bundle component: <artifact id>")."""
    expected = manifest["build"]["bundleComponent"]["sha256"]
    if sha256_bytes(data) != expected:
        raise ReleaseInputsCorrupt("bundle component: object sha256 mismatch")
    component = json.loads(data)
    if component.get("schema") != BUNDLE_SCHEMA:
        raise ReleaseInputsCorrupt("bundle component: schema")
    embedded = _embedded_hashes(component)
    declared = {a: h for a, h in manifest["artifacts"].items()
                if a.startswith("instructions/") or a in {"model", "orchestration"}
                or (a.startswith("tool/") and a.rsplit("/", 1)[1] in {"meta", "schema", "description"})}
    for aid in sorted(set(declared) | set(embedded)):
        if embedded.get(aid) != declared.get(aid):
            raise ReleaseInputsCorrupt(f"bundle component: {aid}")
    return component


def bundle_reuse(component_sha256: str, current: dict | None, applied_inputs: dict | None, stage: str) -> str | None:
    """Bundle version to reuse by content, else None.

    `current` is the stage's deployed `/current` manifest; `applied_inputs` is the release
    inputs document named by `/applied.releaseInputsUri` (already sha-verified)."""
    bundle = (((current or {}).get("deployment") or {}).get(stage) or {}).get("bundle") or {}
    if bundle.get("componentSha256") == component_sha256 and bundle.get("versionId"):
        return bundle["versionId"]
    applied = applied_inputs or {}
    if ((applied.get("bundleComponent") or {}).get("sha256") == component_sha256 and applied.get("bundleVersion")):
        return applied["bundleVersion"]
    return None


# ---------------------------------------------------------------- runtime identity
def runtime_document(config: dict) -> dict:
    """The canonical runtime-version configuration: caller-settable members only, absent omitted."""
    return {k: config[k] for k in RUNTIME_CONFIG_MEMBERS if k in config and config[k] is not None}


def runtime_ids(config: dict, manifest: dict, stage: str) -> tuple[str, str]:
    """(runtimeConfigSha256, runtimeIdentity) for a GetAgentRuntime(version) response.

    The identity replaces three version numbers with the content they point to: each
    toolArns[t] by build.handlers[t].codeSha256, bundleVersion by the bundle componentSha256,
    and guardrail.version by guardrail.definitionSha256."""
    doc = runtime_document(config)
    config_sha = json_hash(doc)
    ident = copy.deepcopy(doc)
    env = (ident.get("environmentVariables") or {})
    raw = env.get(RUNTIME_CONFIG_ENV)
    if raw:
        rc = json.loads(raw)
        handlers = (manifest.get("build") or {}).get("handlers") or {}
        rc["toolArns"] = {t: (handlers.get(t) or {}).get("codeSha256", arn) for t, arn in (rc.get("toolArns") or {}).items()}
        deployment = ((manifest.get("deployment") or {}).get(stage) or {})
        component = (deployment.get("bundle") or {}).get("componentSha256") or \
            ((manifest.get("build") or {}).get("bundleComponent") or {}).get("sha256")
        if "bundleVersion" in rc and component:
            rc["bundleVersion"] = component
        definition = (manifest.get("guardrail") or {}).get("definitionSha256")
        if isinstance(rc.get("guardrail"), dict) and definition:
            rc["guardrail"] = {**rc["guardrail"], "version": definition}
        env[RUNTIME_CONFIG_ENV] = canonical_json(rc).decode("utf-8")
    return config_sha, json_hash(ident)


# ---------------------------------------------------------------- declared manifest
def code_sha256(zip_bytes: bytes) -> str:
    """Lambda's CodeSha256 form: base64 of the raw sha256 digest."""
    return base64.b64encode(hashlib.sha256(zip_bytes).digest()).decode("ascii")


def _guardrail_definition(root: Path) -> str | None:
    try:
        from . import guardrail as projection
    except ImportError:
        return None
    return projection.definition_sha256(cfg.guardrail(root))


def build_declared(repo_root: str | Path, git: dict, artifact_set: A.ArtifactSet | None = None,
                   with_handlers: bool = True) -> dict:
    """The declared manifest for a checkout. `git` = {commit, timestamp (UTC Z), tag, subject}."""
    root = Path(repo_root)
    aset = artifact_set or A.compute(root)
    mv = manifest_version(git["timestamp"], git["commit"])
    models = cfg.models(root)
    orch = cfg.orchestration(root)
    component = bundle_component(root)
    handlers = {}
    if with_handlers:
        for tool in closures.lambda_tools(root):
            data = closures.handler_zip(tool, root)
            handlers[tool] = {"codeSha256": code_sha256(data),
                              "s3Key": f"handlers/{tool}/{hashlib.sha256(data).hexdigest()}.zip"}
    flags = cfg.read(root, A.SINGLE["flags"])
    surfaces = cfg.external_surfaces(root)
    thresholds = cfg.thresholds(root)
    manifest = {
        "schema": SCHEMA,
        "manifestVersion": mv,
        "harness": "devops-companion",
        "git": {"commit": git["commit"], "tag": git.get("tag"), "subject": git.get("subject", "")},
        "digests": aset.digests(),
        "artifacts": dict(sorted(aset.hashes.items())),
        "model": {a: {k: s[k] for k in ("modelId", "maxOutputTokens", "reasoningEffort") if k in s}
                  for a, s in sorted(models.items())},
        "orchestration": {k: orch[k] for k in ("maxTurns", "maxToolCalls", "deadlineSeconds")},
        "tools": {t: {"timeoutSeconds": component["tools"][t]["meta"].get("timeoutSeconds"),
                      "memoryMb": component["tools"][t]["meta"].get("memoryMb")} for t in sorted(component["tools"])},
        "retrieval": [{k: c.get(k) for k in ("corpus", "snapshotId", "snapshotSha256", "documentCount", "builtFrom")}
                      for c in cfg.retrieval(root)],
        "externalSurfaces": [{"id": s["id"], "kind": s["kind"], "sha256": s.get("sha256")} for s in surfaces],
        "build": {"imageDigest": None, "imageContext": aset.hashes["runtime/code"], "layers": {},
                  "handlers": handlers,
                  "bundleComponent": {"s3Key": f"releases/{mv}/bundle-component.json",
                                      "sha256": sha256_bytes(component_bytes(component))}},
        "flags": {"sha256": aset.hashes["flags"], "flagKeys": sorted((flags or {}).get("flags", {}))},
        "guardrail": {"definitionSha256": _guardrail_definition(root)},
        "evaluation": {"suiteSha256": aset.hashes["evaluation/suite"],
                       "evaluatorSetVersion": thresholds["evaluatorSetVersion"],
                       "baselineManifest": thresholds.get("baselineManifest")},
    }
    validate(manifest)
    return manifest


def hash_set(manifest: dict) -> dict[str, str]:
    return dict(manifest.get("artifacts") or {})


# ---------------------------------------------------------------- compact / schema
def compact(manifest: dict, s3_uri: str) -> dict:
    """The SSM `/current` form: `artifacts` replaced by artifactsSha256 + s3Uri; <= 7,500 bytes."""
    out = {k: v for k, v in manifest.items() if k != "artifacts"}
    out["artifactsSha256"] = json_hash(manifest.get("artifacts") or {})
    out["s3Uri"] = s3_uri
    size = len(canonical_json(out))
    if size > MAX_COMPACT_BYTES:
        raise ManifestTooLarge(f"compact manifest is {size} bytes (limit {MAX_COMPACT_BYTES})")
    return out


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(manifest: dict) -> None:
    import jsonschema   # lazy: the drift checker Lambda reads manifests without validating them
    try:
        jsonschema.Draft202012Validator(schema()).validate(manifest)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise ManifestError(f"manifest invalid at {where}: {exc.message}") from None


def explain_normalization(repo_root: str | Path, artifact_set: A.ArtifactSet) -> list[dict]:
    """Per artifact, the files and their normalized hashes (for `manifest build --explain-normalization`)."""
    return [{"artifact": a, "files": artifact_set.file_hashes.get(a, {}), "hash": artifact_set.hashes[a]}
            for a in artifact_set.ids()]


def diff(old: dict | None, new: dict) -> dict[str, list[str]]:
    """Artifact-level diff between two manifests (or hash sets)."""
    o = hash_set(old) if old and "artifacts" in old else (old or {})
    n = hash_set(new) if "artifacts" in new else new
    return {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)),
            "changed": sorted(a for a in set(n) & set(o) if n[a] != o[a])}


ReadObject = Callable[[str], Any]
