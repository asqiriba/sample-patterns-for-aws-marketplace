# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bot change requests: deterministic branch, reuse of an open MR, a leftover
branch restarted, Commits-API actions, and never a merge request to GitLab from open_change itself."""
from __future__ import annotations

import base64

import pytest

from module11.aws import bot_changes as B
from module11.cli_eval import baseline_branch, with_baseline_manifest

SHA = "d" * 40


class FakeGitLab:
    def __init__(self, open_mrs=(), branch_exists=False, files=()):
        self.open_mrs, self.branch_exists, self.files = list(open_mrs), branch_exists, set(files)
        self.calls = []

    def merge_requests(self, source_branch=None, state="opened"):
        return self.open_mrs

    def branch(self, name):
        return {"name": name} if self.branch_exists else None

    def delete_branch(self, name):
        self.calls.append(("delete_branch", name))

    def file(self, path, ref):
        return b"old" if path in self.files else None

    def commit_actions(self, branch, message, actions, start_branch=None):
        self.calls.append(("commit", branch, start_branch, [(a["action"], a["file_path"]) for a in actions]))
        return {"id": SHA}

    def create_merge_request(self, source, title, description, labels, target="main"):
        self.calls.append(("mr", source, target, list(labels)))
        return {"iid": 9, "web_url": "https://gitlab.example/mr/9"}

    def merge(self, iid, sha, when_pipeline_succeeds=False):
        self.calls.append(("merge", iid, sha, when_pipeline_succeeds))


def test_opens_a_new_change_with_update_actions_and_no_merge_by_default():
    gl = FakeGitLab(files={"module11/harness/evaluation/thresholds.yaml"})
    cr = B.open_change(gl, branch="demo/baseline-0123456789abcdef", files={
        "module11/harness/evaluation/thresholds.yaml": b"new"}, message="m", title="t", description="d",
        labels=["m11-demo-run"])
    assert (cr.iid, cr.reused, cr.sha) == (9, False, SHA)
    assert gl.calls == [("commit", "demo/baseline-0123456789abcdef", "main",
                         [("update", "module11/harness/evaluation/thresholds.yaml")]),
                        ("mr", "demo/baseline-0123456789abcdef", "main", ["m11-demo-run"])]


def test_reuses_an_open_mr_and_restarts_a_leftover_branch():
    gl = FakeGitLab(open_mrs=[{"iid": 4, "web_url": "u", "sha": SHA}])
    cr = B.open_change(gl, branch="scenario/abc", files={"a": b"x"}, message="m", title="t", description="d")
    assert cr.reused and cr.iid == 4 and gl.calls == []
    gl = FakeGitLab(branch_exists=True)
    B.open_change(gl, branch="corpus/policy/pol-1", files={"a": b"x"}, message="m", title="t", description="d")
    assert gl.calls[0] == ("delete_branch", "corpus/policy/pol-1") and gl.calls[-1][0] == "mr"
    assert not [c for c in gl.calls if c[0] == "merge"]
    assert gl.calls[1][3] == [("create", "a")]


@pytest.mark.parametrize("branch", ["main", "feature/x", "demo/../main", "demo/"])
def test_only_bot_branches(branch):
    with pytest.raises(ValueError):
        B.open_change(FakeGitLab(), branch=branch, files={}, message="m", title="t", description="d")


def test_actions_are_base64_and_sorted():
    acts = B.actions({"b": b"2", "a": b"1"}, existing=["b"])
    assert [(x["action"], x["file_path"]) for x in acts] == [("create", "a"), ("update", "b")]
    assert base64.b64decode(acts[0]["content"]) == b"1"


def test_baseline_branch_and_text():
    assert baseline_branch("2026-10-07T18:00:42Z/279e717").startswith("demo/baseline-")
    assert baseline_branch("x") == baseline_branch("x") and len(baseline_branch("x")) == len("demo/baseline-") + 16
    text = 'x: 1\nbaselineManifest: "old"  # comment kept? no\ny: 2\n'
    assert 'baselineManifest: "new"' in with_baseline_manifest(text, "new")
