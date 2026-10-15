# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The trusted preview builds' checks: validated inputs, MR targets main
from this project, destroy only for closed/merged MRs or stopped environments, and preview-infra's job and approval
checks before any MR code runs. The merged tree keeps main's gate definition."""
from __future__ import annotations

from pathlib import Path

import pytest

from module11 import cli_preview as PV

SHA = "e" * 40
MR = {"targetBranch": "main", "sourceProjectId": 1, "targetProjectId": 1, "state": "opened", "sha": SHA}


def test_inputs_are_validated_and_never_echoed():
    assert PV.inputs({"PREVIEW_ACTION": "deploy", "PREVIEW_IID": "12", "PREVIEW_SHA": SHA}) == ("deploy", 12, SHA)
    for env in ({"PREVIEW_ACTION": "apply", "PREVIEW_IID": "12", "PREVIEW_SHA": SHA},
                {"PREVIEW_ACTION": "deploy", "PREVIEW_IID": "12;rm", "PREVIEW_SHA": SHA},
                {"PREVIEW_ACTION": "deploy", "PREVIEW_IID": "12", "PREVIEW_SHA": "arn:aws:secretsmanager:x"}):
        with pytest.raises(PV.PreviewRejected) as exc:
            PV.inputs(env)
        assert str(exc.value) == "invalid input"


def test_mr_checks():
    PV.check_mr(MR, "deploy", SHA)
    for bad, why in (({**MR, "targetBranch": "release"}, "main"), ({**MR, "sourceProjectId": 2}, "fork"),
                     ({**MR, "state": "merged"}, "merged"), ({**MR, "sha": "f" * 40}, "head")):
        with pytest.raises(PV.PreviewRejected, match=why):
            PV.check_mr(bad, "gate", SHA)


def test_destroy_only_closed_merged_or_stopped():
    PV.check_mr({**MR, "state": "merged"}, "destroy", SHA)
    PV.check_mr({**MR, "state": "closed"}, "destroy", SHA)
    PV.check_mr(MR, "destroy", SHA, {"state": "stopping"})
    with pytest.raises(PV.PreviewRejected, match="open MR's live environment"):
        PV.check_mr(MR, "destroy", SHA, {"state": "available"})


def test_preview_infra_job_and_platform_approval():
    job = {"name": "preview:deploy-infra", "status": "running",
           "pipeline": {"sha": SHA, "ref": "refs/merge-requests/12/head"}}
    deployment = {"found": True, "approvals": [{"userId": 7, "status": "approved"}]}
    PV.check_infra_job(job, deployment, iid=12, sha=SHA, members={7: True})
    with pytest.raises(PV.PreviewRejected, match="platform member"):
        PV.check_infra_job(job, deployment, iid=12, sha=SHA, members={7: False})
    with pytest.raises(PV.PreviewRejected, match="another MR"):
        PV.check_infra_job(job, deployment, iid=13, sha=SHA, members={7: True})
    with pytest.raises(PV.PreviewRejected, match="running"):
        PV.check_infra_job({**job, "name": "preview:deploy"}, deployment, iid=12, sha=SHA, members={7: True})
    with pytest.raises(PV.PreviewRejected):
        PV.check_infra_job(job, {"found": True, "approvals": [{"userId": 7, "status": "pending"}]}, iid=12, sha=SHA,
                           members={7: True})


def test_merged_tree_keeps_mains_gate_and_takes_the_mr_harness(tmp_path):
    tooling, mr = tmp_path / "tooling", tmp_path / "mr"
    for root, prompt, threshold in ((tooling, "main prompt", "minMean: 0.80"), (mr, "MR prompt", "minMean: 0.10")):
        (root / "module11/harness/agent/prompts").mkdir(parents=True)
        (root / "module11/harness/evaluation").mkdir(parents=True)
        (root / "module11/harness/agent/prompts/companion.md").write_text(prompt)
        (root / "module11/harness/evaluation/thresholds.yaml").write_text(threshold)
        (root / "module11/release").mkdir(parents=True)
        (root / "module11/release/gate.py").write_text("main tooling" if root == tooling else "MR tooling")
    out = tmp_path / "merged"
    info = PV.merge_tree(tooling, mr, out)
    assert (out / "module11/harness/agent/prompts/companion.md").read_text() == "MR prompt"
    assert (out / "module11/harness/evaluation/thresholds.yaml").read_text() == "minMean: 0.80"
    assert (out / "module11/release/gate.py").read_text() == "main tooling"
    assert info["proposed"] == ["module11/harness/evaluation/thresholds.yaml"]


def test_cli_registers_the_preview_entry_points():
    from module11.cli import parser
    assert parser().parse_args(["pipeline", "preview"]).fn is PV.cmd_preview
    assert parser().parse_args(["pipeline", "preview-infra-verify"]).fn is PV.cmd_preview_infra_verify
    assert parser().parse_args(["pipeline", "bot-commit", "--change", "0" * 16]).fn is PV.cmd_bot_commit


# ---------------------------------------------------------------- bot-commit: every input checked before git runs
CHANGE = {"branch": "demo/payments-tweak", "message": "demo arm: one edit", "author": "m11 bot",
          "email": "m11-bot@noreply.gitlab.com", "files": {"module11/harness/agent/prompts/companion.md": "aGk="}}


def test_gitlab_project_path_is_checked():
    for good in ("example-group/devops-companion", "example-group/sub.group/project_1"):
        assert PV.gitlab_project(good) == good
    for bad in ("", "project", "-x/project", "group/../project", "group/pro ject", "group/p;rm -rf",
                "https://gitlab.example/group/project", "group/project\n", ".hidden/project"):
        with pytest.raises(ValueError, match="M11_GITLAB_PROJECT"):
            PV.gitlab_project(bad)


def test_bot_change_paths_and_fields_are_checked():
    assert PV.bot_change(CHANGE)["branch"] == "demo/payments-tweak"
    assert PV.bot_change({k: v for k, v in CHANGE.items() if k not in ("author", "email")})["author"] == "m11 bot"
    for path in ("/etc/passwd", "../outside.txt", "module11/../../x", ".git/config", ".GIT/hooks/pre-commit",
                 "a//b", "./a", "a\\b", ""):
        with pytest.raises(ValueError, match="repository-relative"):
            PV.bot_change({**CHANGE, "files": {path: "aGk="}})
    for field, value, why in (("branch", "main", "bot branches"), ("branch", "demo/../main", "bot branches"),
                              ("branch", "demo/x/", "bot branches"), ("branch", "demo/x\n", "bot branches"),
                              ("files", {}, "at least one file"),
                              ("files", {"a.md": b"raw"}, "base64 text"), ("author", "bot\nX-Evil: 1", "author"),
                              ("author", "--upload-pack=x", "author"), ("email", "not an email", "email"),
                              ("message", "", "message"), ("message", "x" * (PV.MAX_MESSAGE + 1), "message"),
                              ("message", "a\x00b", "message")):
        with pytest.raises(ValueError, match=why):
            PV.bot_change({**CHANGE, field: value})
    with pytest.raises(ValueError):
        PV.bot_change(["not", "a", "dict"])


class _Secrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": "glpat-example" if SecretId.endswith("gitlab-token") else "ssh-key"}


class _Admin:
    def client(self, name):
        return _Secrets() if name == "secretsmanager" else object()


def _bot_commit(monkeypatch, tmp_path, change, *, link_outside=None, calls=None):
    """Run cmd_bot_commit with fakes for S3, Secrets Manager and git; returns the recorded git argv lists."""
    import json
    import types
    from module11 import cli_live
    from module11.aws import releases
    calls = [] if calls is None else calls

    def run(argv, **kwargs):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv), argv
        assert "shell" not in kwargs
        calls.append(argv)
        if "clone" in argv:
            repo = Path(argv[-1])
            repo.mkdir(parents=True)
            if link_outside is not None:
                (repo / "docs").symlink_to(link_outside, target_is_directory=True)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setenv("M11_GITLAB_PROJECT", "example-group/devops-companion")
    monkeypatch.setattr(cli_live, "_context",
                        lambda root: types.SimpleNamespace(admin=_Admin(), release_bucket="example-bucket"))
    monkeypatch.setattr(releases, "get", lambda s3, uri: json.dumps(change).encode())
    monkeypatch.setattr(PV.subprocess, "run", run)
    monkeypatch.setattr(PV.tempfile, "tempdir", str(tmp_path))
    PV.cmd_bot_commit(types.SimpleNamespace(root=tmp_path, change="0123456789abcdef"))
    return calls


def test_bot_commit_runs_git_with_list_args_and_checked_values(monkeypatch, tmp_path):
    calls = _bot_commit(monkeypatch, tmp_path, CHANGE)
    clone = next(c for c in calls if "clone" in c)
    assert "https://gitlab.com/example-group/devops-companion.git" in clone
    commit = next(c for c in calls if "commit" in c)
    assert "user.name=m11 bot" in commit and "user.email=m11-bot@noreply.gitlab.com" in commit
    assert commit[commit.index("-m") + 1] == "demo arm: one edit"
    assert next(c for c in calls if "push" in c)[-1] == "HEAD:demo/payments-tweak"


def test_bot_commit_refuses_bad_input_before_any_git_call(monkeypatch, tmp_path):
    for change in ({**CHANGE, "files": {"../../outside.txt": "aGk="}}, {**CHANGE, "branch": "main"}):
        calls = []
        with pytest.raises(ValueError):
            _bot_commit(monkeypatch, tmp_path, change, calls=calls)
        assert calls == []
    monkeypatch.setenv("M11_GITLAB_PROJECT", "example-group/x;rm")
    with pytest.raises(ValueError, match="M11_GITLAB_PROJECT"):
        PV.cmd_bot_commit(__import__("types").SimpleNamespace(root=tmp_path, change="0123456789abcdef"))
    with pytest.raises(ValueError, match="change ID"):
        PV.cmd_bot_commit(__import__("types").SimpleNamespace(root=tmp_path, change="../../x"))
    with pytest.raises(ValueError, match="change ID"):
        PV.cmd_bot_commit(__import__("types").SimpleNamespace(root=tmp_path, change="0123456789abcdef\n"))


def test_bot_commit_does_not_follow_a_symlink_out_of_the_clone(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="inside the repository"):
        _bot_commit(monkeypatch, tmp_path, {**CHANGE, "files": {"docs/x.md": "aGk="}}, link_outside=outside)
    assert not (outside / "x.md").exists()
