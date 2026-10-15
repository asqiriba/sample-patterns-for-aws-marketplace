# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The single definition of the harness's artifacts.

55 artifacts for the demo harness: 50 in the behavior set (39 of them in the runtime set) and
5 release-only. Paths are relative to the repository root (`agentic-ai/` in the sample repo,
the project root in the GitLab snapshot).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import closure as closures
from .hashing import digest, file_hash, files_hash

HARNESS = "module11/harness"
AGENT = f"{HARNESS}/agent"
TOOLS = f"{AGENT}/tools"
TOOL_FILES = {"meta": "tool.json", "schema": "schema.json", "description": "description.md",
              "permissions": "permissions.json"}
SINGLE = {
    "orchestration": f"{AGENT}/orchestration.yaml",
    "model": f"{AGENT}/models.yaml",
    "guardrail": f"{AGENT}/guardrail.yaml",
    "flags": f"{HARNESS}/flags/flags.json",
    "retrieval": f"{AGENT}/retrieval.yaml",
    "external_surfaces": f"{AGENT}/external_surfaces.yaml",
}
MODULE10_INCIDENT = "module10/fixtures/incident.json"
LAMBDA_REQUIREMENTS = "module11/requirements-lambda.txt"
RUNTIME_EXTRA = ["module11/deployment/Dockerfile", "module11/requirements-runtime.txt"]
INFRA_DIRS = ["module11/infrastructure", "module11/lambdas", "module11/pipeline/buildspecs", "module11/gitlab",
              "module11/vercel"]
GOLDEN = "tests/fixtures/module11/golden.json"
_IGNORED_PARTS = {"__pycache__", "venv", ".pytest_cache", "node_modules"}
_STACK_FILE = re.compile(r"^Pulumi\.[^.]+\.yaml$")


class ArtifactError(ValueError):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True)
class Artifact:
    id: str
    set: str               # behavior | release
    runtime: bool          # in the runtime set (digests.runtime)
    multi: bool            # multi-file artifact (sorted relpath\0hash lines)
    sources: tuple[str, ...]


@dataclass
class ArtifactSet:
    artifacts: dict[str, Artifact]
    hashes: dict[str, str]
    file_hashes: dict[str, dict[str, str]] = field(default_factory=dict)

    def ids(self, which: str = "all") -> list[str]:
        if which == "behavior":
            return sorted(a for a, v in self.artifacts.items() if v.set == "behavior")
        if which == "runtime":
            return sorted(a for a, v in self.artifacts.items() if v.runtime)
        return sorted(self.artifacts)

    def digests(self) -> dict:
        pick = lambda which: {a: self.hashes[a] for a in self.ids(which)}  # noqa: E731
        return {"behavior": digest(pick("behavior")), "release": digest(pick("all")),
                "runtime": digest(pick("runtime")), "artifactCount": len(self.artifacts),
                "behaviorCount": len(self.ids("behavior")), "runtimeCount": len(self.ids("runtime"))}

    def sources(self, which: str = "behavior") -> list[str]:
        return sorted({s for a in self.ids(which) for s in self.artifacts[a].sources})


def _walk(root: Path, rel_dir: str) -> list[str]:
    base = root / rel_dir
    if not base.is_dir():
        return []
    out = []
    for path in base.rglob("*"):
        rel = path.relative_to(root)
        if _IGNORED_PARTS & set(rel.parts) or path.is_symlink() or not path.is_file() or path.suffix == ".pyc":
            continue
        if _STACK_FILE.match(rel.name):
            continue        # Pulumi.<stack>.yaml: git-ignored, per-account stack config (absent in CI)
        out.append(rel.as_posix())
    return sorted(out)


def agents(root: Path) -> list[str]:
    return sorted(p.stem for p in (root / AGENT / "prompts").glob("*.md"))


def tool_names(root: Path) -> list[str]:
    base = root / TOOLS
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))) if base.is_dir() else []


def tool_kind(root: Path, tool: str) -> str:
    meta = root / TOOLS / tool / "tool.json"
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("kind", "lambda")
    except (OSError, ValueError):
        return "lambda" if (root / TOOLS / tool / "handler").is_dir() else "delegate"


def define(repo_root: str | Path) -> dict[str, Artifact]:
    """Artifact IDs and their source files, from the harness layout."""
    root = Path(repo_root)
    arts: dict[str, Artifact] = {}

    def add(aid, set_, runtime, multi, sources):
        arts[aid] = Artifact(aid, set_, runtime, multi, tuple(sorted(sources)))

    for agent in agents(root):
        add(f"instructions/{agent}", "behavior", True, False, [f"{AGENT}/prompts/{agent}.md"])
    for tool in tool_names(root):
        for part, name in TOOL_FILES.items():
            add(f"tool/{tool}/{part}", "behavior", part != "permissions", False, [f"{TOOLS}/{tool}/{name}"])
        if tool_kind(root, tool) == "lambda":
            listed = closures.read_list(root, f"handler-{tool}.txt")
            files = listed if listed is not None else closures.handler_closure(tool, root)
            add(f"tool/{tool}/handler", "behavior", True, True, [*files, LAMBDA_REQUIREMENTS])
    for aid, path in SINGLE.items():
        add(aid, "behavior", aid not in {"flags", "external_surfaces"}, False, [path])
    add("evidence", "behavior", True, True, [*_walk(root, f"{HARNESS}/evidence"), MODULE10_INCIDENT])
    runtime_files = closures.read_list(root, "runtime.txt") or []
    add("runtime/code", "behavior", True, True, [*runtime_files, *RUNTIME_EXTRA])
    add("edge/code", "behavior", False, True, closures.edge_files(root))
    add("evaluation/suite", "release", False, True, _walk(root, f"{HARNESS}/evaluation/scenarios"))
    add("evaluation/evaluators", "release", False, True,
        [f"{HARNESS}/evaluation/evaluators.yaml", *_walk(root, f"{HARNESS}/evaluation/evaluators")])
    add("evaluation/thresholds", "release", False, True,
        [f"{HARNESS}/evaluation/thresholds.yaml", f"{HARNESS}/evaluation/prices.yaml"])
    add("pipeline", "release", False, True,
        [f"{HARNESS}/pipeline/change_types.yaml", *_walk(root, f"{HARNESS}/pipeline/closures")])
    add("infrastructure", "release", False, True, [p for d in INFRA_DIRS for p in _walk(root, d)])
    return arts


def compute(repo_root: str | Path) -> ArtifactSet:
    """Hash every artifact. Missing single-file sources raise ArtifactError; missing optional
    multi-file members (for example a missing Dockerfile) are skipped."""
    root = Path(repo_root)
    arts = define(root)
    hashes: dict[str, str] = {}
    per_file: dict[str, dict[str, str]] = {}
    for aid, art in arts.items():
        fh: dict[str, str] = {}
        for src in art.sources:
            path = root / src
            if not path.is_file():
                if art.multi:
                    continue
                raise ArtifactError(src, "missing")
            fh[src] = file_hash(src, path.read_bytes())
        per_file[aid] = fh
        hashes[aid] = files_hash(fh) if art.multi else next(iter(fh.values()))
    return ArtifactSet(arts, hashes, per_file)


def owner_of(artifact_set: ArtifactSet, path: str) -> list[str]:
    """Artifact IDs whose sources include `path`."""
    return sorted(a for a, art in artifact_set.artifacts.items() if path in art.sources)


def changed_ids(artifact_set: ArtifactSet, changed_paths: list[str]) -> list[str]:
    return sorted({a for p in changed_paths for a in owner_of(artifact_set, p)})


def golden_counts(repo_root: str | Path) -> dict | None:
    path = Path(repo_root) / GOLDEN
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("counts")
