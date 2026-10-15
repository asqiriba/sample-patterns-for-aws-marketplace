# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which files an MR preview takes from `main` and which from the MR.

The trusted preview build runs `main`'s tooling archive on the MR's data. The whole gate
definition (`module11/harness/evaluation/**`: scenarios, thresholds, evaluators, the
code-based evaluator's code, prices) comes from the tooling tree, so an MR cannot loosen its
own gate. From the MR come only `module11/harness/**` outside `harness/evaluation/`, and the
runtime image context and tool handler closures those files name. `proposed` holds the MR's
own differing evaluation files, which feed only the MR note's informational table.

Trees are {repo-relative path: bytes}; `read_tree(dir)` builds one from a checkout.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .artifacts import HARNESS, RUNTIME_EXTRA
from .closure import CLOSURES_DIR, parse_list

EVALUATION = f"{HARNESS}/evaluation/"
EVALUATOR_CODE = f"{HARNESS}/evaluation/evaluators/"
SCENARIOS = f"{HARNESS}/evaluation/scenarios/"
_SKIP_PARTS = {"__pycache__", ".pytest_cache", "venv", "node_modules", ".git"}

Tree = Mapping[str, bytes]


@dataclass
class PreviewInputs:
    gate: dict[str, bytes]                  # main's harness/evaluation/**
    harness: dict[str, bytes]               # the MR's data: harness (minus evaluation) + runtime/handler closures
    proposed: dict[str, bytes]              # the MR's added or changed evaluation files
    removed: list[str] = field(default_factory=list)   # evaluation files the MR deletes
    evaluatorCode: dict[str, bytes] = field(default_factory=dict)   # sealed from the tooling tree

    @property
    def evaluator_code_changed(self) -> bool:
        return any(p.startswith(EVALUATOR_CODE) for p in list(self.proposed) + self.removed)

    def changed_scenarios(self) -> list[str]:
        """IDs of scenarios the MR adds or changes, sorted (the informational run)."""
        main = _scenario_rows(self.gate)
        mine = _scenario_rows({**self.gate, **self.proposed})
        for path in self.removed:
            mine = {k: v for k, v in mine.items() if v[0] != path}
        return sorted(sid for sid, (_, row) in mine.items() if main.get(sid, (None, None))[1] != row)


def read_tree(directory: str | Path) -> dict[str, bytes]:
    base = Path(directory)
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*"))
            if p.is_file() and not (_SKIP_PARTS & set(p.relative_to(base).parts)) and p.suffix != ".pyc"}


def _closure_paths(tree: Tree) -> set[str]:
    paths: set[str] = set()
    for name, data in tree.items():
        if name.startswith(CLOSURES_DIR + "/") and name.endswith(".txt") and not name.endswith("/edge.txt"):
            paths.update(parse_list(data.decode("utf-8")))
    return paths


def split(tooling_tree: Tree, mr_tree: Tree) -> PreviewInputs:
    gate = {p: b for p, b in tooling_tree.items() if p.startswith(EVALUATION)}
    harness = {p: b for p, b in mr_tree.items() if p.startswith(HARNESS + "/") and not p.startswith(EVALUATION)}
    for path in sorted(_closure_paths(harness) | set(RUNTIME_EXTRA)):
        if path.startswith(EVALUATION):
            continue          # never let a closure list pull MR evaluation files into the gate
        if path in mr_tree:
            harness[path] = mr_tree[path]
    mine = {p: b for p, b in mr_tree.items() if p.startswith(EVALUATION)}
    proposed = {p: b for p, b in mine.items() if gate.get(p) != b}
    removed = sorted(set(gate) - set(mine))
    code = {p: b for p, b in gate.items() if p.startswith(EVALUATOR_CODE)}
    return PreviewInputs(gate, harness, proposed, removed, code)


def _scenario_rows(tree: Tree) -> dict[str, tuple[str, dict]]:
    rows: dict[str, tuple[str, dict]] = {}
    for path in sorted(tree):
        if not (path.startswith(SCENARIOS) and path.endswith(".jsonl")):
            continue
        for line in tree[path].decode("utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["scenario_id"]] = (path, row)
    return rows
