# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shadow experiment's fixed steps: input validation, the
freeze binding, the scope rule, the experiment key layout, the shadow-evaluate timing, and the re-runnable
finish order. Offline."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from module11 import cli_experiment as X
from module11.release import seal as SEAL

SHA = "c" * 40
XID = "x-0123456789abcdef"


def test_ids_and_inputs():
    assert X.EXPERIMENT_ID.match(X.new_id())
    X.check_inputs(SHA, XID)
    for sha, xid in (("C" * 40, XID), (SHA, "x-0123"), (SHA, "experiment"), ("", XID), (SHA, None)):
        with pytest.raises(X.ExperimentRefused) as exc:
            X.check_inputs(sha, xid)
        assert str(exc.value) == "invalid input"          # never echoes the value


def test_the_build_requires_this_experiments_freeze():
    assert X.check_freeze({"state": "planned", "reason": f"experiment {XID}", "stage": "dev"}, XID) == "dev"
    for freeze in ({"state": "none"}, {"state": "planned", "reason": "experiment x-ffffffffffffffff", "stage": "dev"},
                   {"state": "emergency", "reason": f"experiment {XID}", "stage": "dev"},
                   {"state": "planned", "reason": f"experiment {XID}", "stage": "staging"}):
        with pytest.raises(X.ExperimentRefused):
            X.check_freeze(freeze, XID)


def test_only_bundle_and_model_changes_may_run():
    X.check_scope(["instructions"])
    X.check_scope(["model", "tool_description", "orchestration"])
    for kinds in (["flags"], ["instructions", "edge_code"], ["runtime_code"], ["tool_handler"], ["infrastructure"], []):
        with pytest.raises(X.ExperimentScopeRejected):
            X.check_scope(kinds)


def test_experiment_objects_never_share_a_release_key(tmp_path):
    declared = {"manifestVersion": "2026-10-07T00:00:00Z/abcdef1",
                "build": {"bundleComponent": {"s3Key": "releases/2026-10-07T00:00:00Z/abcdef1/bundle-component.json",
                                              "sha256": "sha256:x"}}}
    moved = X.experiment_declared(declared)
    assert moved["build"]["bundleComponent"]["s3Key"] == \
        "releases/experiments/2026-10-07T00:00:00Z/abcdef1/bundle-component.json"
    assert declared["build"]["bundleComponent"]["s3Key"].startswith("releases/2026")   # input untouched
    import inspect
    assert "prefix" in inspect.signature(SEAL.seal_objects).parameters


def test_archive_extraction_strips_the_top_directory_and_refuses_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"devops-companion-harness-{SHA}/module11/harness/agent/models.yaml", "x: 1\n")
    tree = X.extract_archive(buf.getvalue(), tmp_path / "a")
    assert (tree / "module11/harness/agent/models.yaml").read_text() == "x: 1\n"
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("top/../../etc/passwd", "no")
    with pytest.raises(X.ExperimentRefused):
        X.extract_archive(bad.getvalue(), tmp_path / "b")


def test_shadow_evaluate_is_sent_once_after_hours():
    assert not X.should_evaluate(1000.0, 1000.0 + 0.25 * 3600 - 1, 0.25, False)
    assert X.should_evaluate(1000.0, 1000.0 + 0.25 * 3600, 0.25, False)
    assert not X.should_evaluate(1000.0, 1000.0 + 3600, 0.25, True)


@pytest.mark.parametrize("state,steps", [
    ("SHADOWING", ["abort", "wait-rolled-back", "reset", "reapply", "unfreeze"]),
    ("ROLLED_BACK", ["reset", "reapply", "unfreeze"]),
    ("IDLE", ["reapply", "unfreeze"]),
])
def test_finish_is_rerunnable_from_any_state(state, steps):
    assert X.finish_steps(state) == steps


def test_timestamps_become_manifest_form():
    assert X.utc_z("2026-10-07T14:33:58.000-04:00") == "2026-10-07T18:33:58Z"
    assert X.utc_z("2026-10-07T18:33:58Z") == "2026-10-07T18:33:58Z"


def test_cli_wires_the_experiment_and_its_report():
    from module11.cli import parser
    args = parser().parse_args(["shadow", "experiment", "--source", "experiment/brevity", "--stage", "dev",
                                "--hours", "0.25"])
    assert args.fn is X.cmd_shadow_experiment and args.hours == 0.25
    args = parser().parse_args(["shadow", "experiment", "--finish", XID])
    assert args.finish == XID
    assert parser().parse_args(["pipeline", "seal-experiment"]).fn is X.cmd_seal_experiment
    with pytest.raises(SystemExit):
        parser().parse_args(["shadow", "experiment", "--source", "b", "--stage", "staging"])
