# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sealed behavior inputs for the harness Pulumi program.

`build_archive()` writes `pulumi-inputs.tar` deterministically (sorted entries, mtime 0,
uid/gid 0, mode 0644, normalized contents) so its sha256 identifies the inputs exactly.
`ReleaseInputs` is the record `release deploy` writes and the program loads, in this order:
M11_RELEASE_INPUTS file, then `/applied` -> releaseInputsUri (sha-verified), else
ReleaseInputsMissing.
"""
from __future__ import annotations

import io
import json
import tarfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping

from . import artifacts as A
from .hashing import sha256_bytes
from .manifest import ReleaseInputsCorrupt
from .normalize import normalize

ENV_VAR = "M11_RELEASE_INPUTS"


class ReleaseInputsMissing(RuntimeError):
    pass


def archive_members(repo_root: str | Path) -> list[tuple[str, str]]:
    """[(archive name, repo path)]: tools/*/tool.json, tools/*/permissions.json, models.yaml,
    guardrail.yaml, orchestration.yaml."""
    root = Path(repo_root)
    members = []
    for tool in A.tool_names(root):
        for name in ("tool.json", "permissions.json"):
            members.append((f"tools/{tool}/{name}", f"{A.TOOLS}/{tool}/{name}"))
    for name, key in (("models.yaml", "model"), ("guardrail.yaml", "guardrail"), ("orchestration.yaml", "orchestration")):
        members.append((name, A.SINGLE[key]))
    return sorted(members)


def build_archive(repo_root: str | Path) -> bytes:
    root = Path(repo_root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, rel in archive_members(root):
            data = normalize(rel, (root / rel).read_bytes())
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def read_archive(data: bytes, expected_sha256: str) -> dict[str, Any]:
    """Verify and parse a sealed archive; members are canonical JSON documents."""
    if sha256_bytes(data) != expected_sha256:
        raise ReleaseInputsCorrupt("pulumi-inputs.tar sha256 mismatch")
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            out[member.name] = json.loads(tar.extractfile(member).read())
    return out


@dataclass
class ReleaseInputs:
    imageDigest: str | None
    bundleArn: str | None
    bundleVersion: str | None
    bundleComponent: dict
    handlers: dict
    layer: dict
    edge: dict
    evaluatorCode: dict
    flagsSha256: str
    snapshot: dict
    evidence: dict
    pulumiInputs: dict
    retainedPins: dict
    runtimeDigest: str
    manifestVersion: str
    behaviorDigest: str
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = asdict(self)
        out.pop("extra")
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, doc: Mapping) -> "ReleaseInputs":
        names = {f.name for f in fields(cls)} - {"extra"}
        missing = sorted(n for n in names if n not in doc)
        if missing:
            raise ReleaseInputsCorrupt(f"release inputs missing {missing}")
        if (doc["bundleArn"] is None) != (doc["bundleVersion"] is None):
            raise ReleaseInputsCorrupt("bundleArn and bundleVersion must both be null or both set")
        return cls(**{n: doc[n] for n in names}, extra={k: v for k, v in doc.items() if k not in names})

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def load(stage: str, env: Mapping[str, str], read_file: Callable[[str], bytes],
         get_applied: Callable[[str], Mapping | None], read_s3: Callable[[str], bytes]) -> ReleaseInputs:
    """The program's load order. Readers are injected; this module never calls AWS."""
    path = env.get(ENV_VAR)
    if path:
        return ReleaseInputs.from_dict(json.loads(read_file(path)))
    applied = get_applied(stage)
    if applied:
        data = read_s3(applied["releaseInputsUri"])
        if sha256_bytes(data) != applied["sha256"]:
            raise ReleaseInputsCorrupt("release inputs sha256 differs from /applied")
        return ReleaseInputs.from_dict(json.loads(data))
    raise ReleaseInputsMissing(f"run `module11 release deploy --stage {stage}`")
