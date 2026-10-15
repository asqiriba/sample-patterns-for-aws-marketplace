# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What the Seal stage writes. Pure: no AWS calls.

`seal_objects(tree, manifest, ...)` returns every object a release needs, content-addressed under
the release bucket, plus the seal record `releases/<mv>/seal.json` that `release deploy` turns into
release inputs. `tree` is an export of a commit (`git archive`), never the working copy (M7):
- handler zips `handlers/<tool>/<sha256 hex>.zip` (deterministic, identical CodeSha256 everywhere);
- the edge zip `handlers/edge/<hex>.zip` (closures/edge.txt, Module 10's .mjs packaged unchanged);
- the code-based evaluator `handlers/eval-no-credential-leakage/<hex>.zip`;
- the operations Lambdas `handlers/ops-<name>/<hex>.zip` (controller, shadow worker: each entry's import
  closure at repo paths) and the m11-ops layer `layers/m11-ops/<hex>.zip` (lambdas/requirements.txt);
- the m11-sdk layer `layers/m11-sdk/<hex>.zip` (built by `build_layer`, pip for linux/arm64);
- `releases/<mv>/{pulumi-inputs.tar, bundle-component.json, flags.json, manifest.json, change-plan.json}`;
- evidence `evidence/<hex>/incidents/<service>.json` and the policy snapshot, with `m11-sha256` metadata (M6);
- the image context `builds/<runtime/code hex>.zip` (what the image build turns into the image).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import artifacts as A
from . import closure as closures
from . import manifest as M
from . import pulumi_inputs as PI
from .hashing import hex_of, sha256_bytes

SEAL_SCHEMA = "m11.seal/1"
EVALUATOR = "module11/harness/evaluation/evaluators/no_credential_leakage/handler.py"
OPERATIONS = {"controller": "module11/lambdas/controller/handler.py",
              "shadow_worker": "module11/lambdas/shadow_worker/handler.py",
              "drift_check": "module11/lambdas/drift_check/handler.py"}
CORE_OPERATIONS = ("controller", "shadow_worker")      # a tree without the later ones still seals (an experiment)
OPS_REQUIREMENTS = "module11/lambdas/requirements.txt"
SMOKE_SCENARIO = "incident-triage-014"
LAYER_PLATFORMS = ("manylinux_2_28_aarch64", "manylinux2014_aarch64")


@dataclass
class SealedObject:
    key: str
    body: bytes
    metadata: dict = field(default_factory=dict)
    content_type: str = "application/octet-stream"

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.body)


def _hex_zip(prefix: str, data: bytes) -> str:
    return f"{prefix}/{hashlib.sha256(data).hexdigest()}.zip"


def edge_zip(tree: Path) -> bytes:
    return closures.deterministic_zip([(name, (tree / path).read_bytes()) for path, name in closures.edge_entries(tree)])


def evaluator_zip(tree: Path) -> bytes:
    return closures.deterministic_zip([("handler.py", (tree / EVALUATOR).read_bytes())])


def operations_zip(tree: Path, name: str) -> bytes:
    """One operations Lambda: its entry's import closure at repo paths (handler `module11.lambdas.<name>.handler`)."""
    files = closures.closure(OPERATIONS[name], tree)
    if OPERATIONS[name] not in files:
        raise M.ReleaseInputsCorrupt(f"{OPERATIONS[name]} is missing from the tree")
    return closures.deterministic_zip([(path, (tree / path).read_bytes()) for path in files])


def has_operations(tree: Path) -> bool:
    return all((tree / path).is_file() for path in (*(OPERATIONS[n] for n in CORE_OPERATIONS), OPS_REQUIREMENTS))


def image_context(tree: Path) -> bytes:
    """Exactly the runtime closure plus the Dockerfile and requirements-runtime.txt, at repo paths."""
    files = sorted(set(closures.read_list(tree, "runtime.txt") or []) | set(A.RUNTIME_EXTRA))
    return closures.deterministic_zip([(path, (tree / path).read_bytes()) for path in files])


def build_layer(requirements: Path, *, python: str = sys.executable) -> bytes:
    """The m11-sdk layer: `python/` with requirements-lambda.txt installed for python3.13 arm64."""
    with tempfile.TemporaryDirectory(prefix="m11-layer-") as tmp:
        target = Path(tmp) / "python"
        # Resolve wheels for linux/arm64 (dependencies included), then unpack them without an index.
        deps = [python, "-m", "pip", "download", "--quiet", "--only-binary=:all:", "--implementation", "cp",
                "--python-version", "3.13", "--dest", str(Path(tmp) / "wheels"), "-r", str(requirements)]
        for platform in LAYER_PLATFORMS:
            deps += ["--platform", platform]
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; pip download with fixed options and repo paths
        subprocess.run(deps, check=True)
        # A wheel is a zip of its installed tree; unpack it under python/ (pip refuses to install a
        # linux wheel on another platform). `.data/` (scripts, headers) is not importable code.
        members: dict[str, bytes] = {}
        for wheel in sorted((Path(tmp) / "wheels").glob("*.whl")):
            with zipfile.ZipFile(wheel) as archive:
                for info in archive.infolist():
                    name = info.filename
                    if info.is_dir() or ".data/" in name.split("/", 1)[0] + "/" or name.endswith(".dist-info/RECORD"):
                        continue
                    members[f"python/{name}"] = archive.read(info)
        shutil.rmtree(target, ignore_errors=True)
        return closures.deterministic_zip(sorted(members.items()))


def _evidence(tree: Path, evidence_sha: str) -> list[SealedObject]:
    sources = {p.stem: p for p in (tree / A.HARNESS / "evidence" / "incidents").glob("*.json")}
    sources["reporting-service"] = tree / A.MODULE10_INCIDENT
    prefix = f"evidence/{hex_of(evidence_sha)}/"
    # index.json lists the services, so a tool told a wrong name answers `not_found` with the real ones
    # (aws/objects.known_services); it is derived from the files below, so it is not part of the evidence hash.
    index = json.dumps({"services": sorted(sources)}, sort_keys=True).encode("utf-8")
    return [SealedObject(prefix + "index.json", index, {"m11-sha256": evidence_sha}, "application/json")] + \
        [SealedObject(f"{prefix}incidents/{service}.json", src.read_bytes(), {"m11-sha256": evidence_sha},
                      "application/json") for service, src in sorted(sources.items())]


def smoke_input(tree: Path) -> dict:
    """incident-triage-014's first turn and the companion's required tools."""
    from . import stage as cfg
    for row in cfg.scenarios(tree):
        if row["scenario_id"] == SMOKE_SCENARIO:
            return {"scenarioId": SMOKE_SCENARIO, "input": row["turns"][0]["input"], "team": row.get("team", "platform"),
                    "service": row.get("service")}
    raise ValueError(f"scenario {SMOKE_SCENARIO} not found")


def seal_objects(tree: Path, manifest: dict, *, image_digest: str, layer_zip: bytes, change_plan: dict,
                 sealed_by: str, sealed_at: str, ops_layer_zip: bytes | None = None, prefix: str = "releases"
                 ) -> tuple[list[SealedObject], dict, dict]:
    """(objects, seal record, final manifest). The manifest gains build.imageDigest and build.layers.
    `prefix`: `releases` for a release, `releases/experiments` for the shadow experiment, whose
    per-release objects never mix with releases; hash-addressed objects are shared either way."""
    from module11.corpus import snapshot as S
    mv = manifest["manifestVersion"]
    objects: list[SealedObject] = []
    handlers = {}
    for tool, ref in sorted(manifest["build"]["handlers"].items()):
        data = closures.handler_zip(tool, tree)
        if M.code_sha256(data) != ref["codeSha256"]:
            raise M.ReleaseInputsCorrupt(f"handler {tool}: the tree's zip differs from the manifest")
        objects.append(SealedObject(ref["s3Key"], data, content_type="application/zip"))
        handlers[tool] = {"s3Key": ref["s3Key"], "codeSha256": ref["codeSha256"]}
    edge = edge_zip(tree)
    evaluator = evaluator_zip(tree)
    layer_key = _hex_zip("layers/m11-sdk", layer_zip)
    objects += [SealedObject(_hex_zip("handlers/edge", edge), edge, content_type="application/zip"),
                SealedObject(_hex_zip("handlers/eval-no-credential-leakage", evaluator), evaluator,
                             content_type="application/zip"),
                SealedObject(layer_key, layer_zip, content_type="application/zip")]
    operations = None
    if ops_layer_zip is not None:
        operations = {"layer": {"s3Key": _hex_zip("layers/m11-ops", ops_layer_zip), "sha256": sha256_bytes(ops_layer_zip)}}
        objects.append(SealedObject(operations["layer"]["s3Key"], ops_layer_zip, content_type="application/zip"))
        for name in sorted(OPERATIONS):
            if name not in CORE_OPERATIONS and not (tree / OPERATIONS[name]).is_file():
                continue
            data = operations_zip(tree, name)
            key = _hex_zip(f"handlers/ops-{name.replace('_', '-')}", data)
            objects.append(SealedObject(key, data, content_type="application/zip"))
            operations[name] = {"s3Key": key, "codeSha256": M.code_sha256(data)}
    tar = PI.build_archive(tree)
    component = M.component_bytes(M.bundle_component(tree))
    if sha256_bytes(component) != manifest["build"]["bundleComponent"]["sha256"]:
        raise M.ReleaseInputsCorrupt("bundle component: the tree differs from the manifest")
    flags = (tree / A.SINGLE["flags"]).read_bytes()
    snapshot_bytes, pin = S.build(tree)
    retrieval = {c["corpus"]: c for c in manifest.get("retrieval", [])}.get("policy") or {}
    if retrieval.get("snapshotSha256") != pin["snapshotSha256"]:
        raise M.ReleaseInputsCorrupt("snapshot: retrieval.yaml pins a different snapshot than the tree builds")
    evidence_sha = manifest["artifacts"]["evidence"]
    objects += [SealedObject(f"{prefix}/{mv}/pulumi-inputs.tar", tar, content_type="application/x-tar"),
                SealedObject(manifest["build"]["bundleComponent"]["s3Key"], component, content_type="application/json"),
                SealedObject(f"{prefix}/{mv}/flags.json", flags, content_type="application/json"),
                SealedObject(S.object_key(pin["snapshotId"]), snapshot_bytes, {"m11-sha256": pin["snapshotSha256"]},
                             "application/json"),
                *_evidence(tree, evidence_sha)]
    final = json.loads(json.dumps(manifest))
    final["build"]["imageDigest"] = image_digest
    final["build"]["layers"] = {"m11-sdk": sha256_bytes(layer_zip)}
    M.validate(final)
    manifest_bytes = json.dumps(final, indent=2, sort_keys=True).encode("utf-8")
    plan_bytes = json.dumps(change_plan, indent=2, sort_keys=True).encode("utf-8")
    objects += [SealedObject(f"{prefix}/{mv}/manifest.json", manifest_bytes, content_type="application/json"),
                SealedObject(f"{prefix}/{mv}/change-plan.json", plan_bytes, content_type="application/json")]
    bucket_uri = "s3://{bucket}/"
    record = {
        "schema": SEAL_SCHEMA, "manifestVersion": mv, "behaviorDigest": final["digests"]["behavior"],
        "runtimeDigest": final["digests"]["runtime"], "imageDigest": image_digest,
        "imageContext": {"s3Key": f"builds/{hex_of(final['build']['imageContext'])}.zip",
                         "sha256": final["build"]["imageContext"]},
        "handlers": handlers, "layer": {"s3Key": layer_key, "sha256": sha256_bytes(layer_zip)},
        "edge": {"s3Key": _hex_zip("handlers/edge", edge), "sha256": sha256_bytes(edge)},
        "evaluatorCode": {"s3Key": _hex_zip("handlers/eval-no-credential-leakage", evaluator),
                          "codeSha256": M.code_sha256(evaluator)},
        "bundleComponent": {"uri": bucket_uri + manifest["build"]["bundleComponent"]["s3Key"],
                            "sha256": manifest["build"]["bundleComponent"]["sha256"]},
        "flags": {"uri": bucket_uri + f"{prefix}/{mv}/flags.json", "sha256": sha256_bytes(flags)},
        "flagsSha256": final["flags"]["sha256"],
        "snapshot": {"id": pin["snapshotId"], "uri": bucket_uri + S.object_key(pin["snapshotId"]),
                     "sha256": pin["snapshotSha256"]},
        "evidence": {"uri": bucket_uri + f"evidence/{hex_of(evidence_sha)}/", "sha256": evidence_sha},
        "pulumiInputs": {"uri": bucket_uri + f"{prefix}/{mv}/pulumi-inputs.tar", "sha256": sha256_bytes(tar)},
        "manifest": {"uri": bucket_uri + f"{prefix}/{mv}/manifest.json", "sha256": sha256_bytes(manifest_bytes)},
        "changePlan": {"uri": bucket_uri + f"{prefix}/{mv}/change-plan.json", "sha256": sha256_bytes(plan_bytes)},
        "smoke": smoke_input(tree), "git": final["git"], "sealedBy": sealed_by, "sealedAt": sealed_at,
    }
    if operations:
        record["operationsCode"] = operations
    return objects, record, final


def bind_bucket(record: dict, bucket: str) -> dict:
    """Replace the `s3://{bucket}/` placeholder with the release bucket's name."""
    return json.loads(json.dumps(record).replace("s3://{bucket}/", f"s3://{bucket}/"))


def release_inputs(record: dict, *, retained_pins: dict, bundle_arn: str | None, bundle_version: str | None) -> PI.ReleaseInputs:
    """The record `release deploy` writes to release-inputs/<stage>/<mv>.json."""
    return PI.ReleaseInputs(
        imageDigest=record["imageDigest"], bundleArn=bundle_arn, bundleVersion=bundle_version,
        bundleComponent=record["bundleComponent"], handlers=record["handlers"], layer=record["layer"],
        edge=record["edge"], evaluatorCode=record["evaluatorCode"], flagsSha256=record["flagsSha256"],
        snapshot=record["snapshot"], evidence=record["evidence"], pulumiInputs=record["pulumiInputs"],
        retainedPins=retained_pins, runtimeDigest=record["runtimeDigest"], manifestVersion=record["manifestVersion"],
        behaviorDigest=record["behaviorDigest"],
        extra={"flags": record["flags"], **({"operationsCode": record["operationsCode"]}
                                            if record.get("operationsCode") else {})})
