# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/artifacts.py."""
import json
import shutil
from pathlib import Path

import pytest

from module11.release import artifacts as A

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"
GOLDEN = json.loads((ROOT / "tests/fixtures/module11/golden.json").read_text(encoding="utf-8"))
NOT_RUNTIME = {"flags", "external_surfaces", "edge/code"}


@pytest.mark.parametrize("root", [FIXTURE, ROOT], ids=["harness_min", "real"])
def test_counts_55_50_39(root):
    aset = A.compute(root)
    d = aset.digests()
    assert (d["artifactCount"], d["behaviorCount"], d["runtimeCount"]) == (55, 50, 39)
    assert (d["artifactCount"], d["behaviorCount"], d["runtimeCount"]) == tuple(GOLDEN["counts"].values())


def test_runtime_set_excludes_stage_wide_artifacts():
    aset = A.compute(FIXTURE)
    runtime = set(aset.ids("runtime"))
    behavior = set(aset.ids("behavior"))
    excluded = behavior - runtime
    assert excluded == {a for a in behavior if a.endswith("/permissions")} | NOT_RUNTIME
    assert len([a for a in excluded if a.endswith("/permissions")]) == 8
    assert {"evaluation/suite", "evaluation/evaluators", "evaluation/thresholds", "pipeline",
            "infrastructure"} == set(aset.ids()) - behavior


def test_golden_digests_are_stable():
    d = A.compute(FIXTURE).digests()
    for key in ("behavior", "runtime", "release"):
        assert d[key] == GOLDEN["harness_min"][key], key


def test_two_computations_are_identical_and_crlf_insensitive(tmp_path):
    root = tmp_path / "h"
    shutil.copytree(FIXTURE, root)
    first = A.compute(root).digests()
    prompt = root / A.AGENT / "prompts/companion.md"
    prompt.write_bytes(prompt.read_bytes().replace(b"\n", b"\r\n"))
    assert A.compute(root).digests() == first
    prompt.write_text(prompt.read_text(encoding="utf-8") + "One more rule.\n", encoding="utf-8")
    changed = A.compute(root)
    assert changed.digests()["behavior"] != first["behavior"]
    assert changed.digests()["runtime"] != first["runtime"]


def test_flags_change_moves_behavior_but_not_runtime_digest(tmp_path):
    root = tmp_path / "h"
    shutil.copytree(FIXTURE, root)
    first = A.compute(root).digests()
    flags = root / A.SINGLE["flags"]
    doc = json.loads(flags.read_text(encoding="utf-8"))
    doc["flags"]["module10-recovery-planning"]["version"] = 2
    flags.write_text(json.dumps(doc), encoding="utf-8")
    after = A.compute(root).digests()
    assert after["behavior"] != first["behavior"] and after["runtime"] == first["runtime"]


def test_sources_and_owner_lookup():
    aset = A.compute(FIXTURE)
    assert A.owner_of(aset, A.MODULE10_INCIDENT) == ["evidence"]
    assert "module11/requirements-lambda.txt" in aset.artifacts["tool/search_runbooks/handler"].sources
    assert A.changed_ids(aset, [f"{A.TOOLS}/query_metrics/tool.json"]) == ["tool/query_metrics/meta"]


def test_missing_single_file_raises(tmp_path):
    root = tmp_path / "h"
    shutil.copytree(FIXTURE, root)
    (root / A.SINGLE["guardrail"]).unlink()
    with pytest.raises(A.ArtifactError):
        A.compute(root)
