# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline CLI smoke tests: tree, manifest build, check, detect on a temporary git repo,
seed --dry-run, and the isolate_module11 fixture itself."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_module11_support import sample_layout_only

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"


def run(*args, cwd=ROOT):
    # Offline: no M11_* settings and no AWS credentials, so an AWS-backed command fails before any call.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("M11_", "AWS_"))}
    env.update({"AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                "AWS_EC2_METADATA_DISABLED": "true", "AWS_REGION": "us-east-1"})
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI with the test's arguments
    return subprocess.run([sys.executable, "-m", "module11", *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=120)


def test_tree_and_manifest_build_are_stable():
    out = run("tree")
    assert out.returncode == 0 and "55 artifacts / 50 behavior / 39 runtime" in out.stdout
    first, second = run("manifest", "build"), run("manifest", "build")
    assert first.returncode == 0 and first.stdout == second.stdout
    assert "55 artifacts / 50 in the behavior set / 39 runtime" in first.stdout


def test_check_passes_on_the_harness():
    out = run("check")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "0 error(s)" in out.stdout


def test_later_commands_name_their_feat():
    for argv in (("captures", "export", "--help"), ("teardown", "--help"), ("demo", "arm", "--help"),
                 ("demo", "rearm", "--help"), ("demo", "prep", "--help")):       
        assert run(*argv).returncode == 0, argv
    out = run("release", "approve", "--execution", "x")
    assert out.returncode == 1 and "--break-glass" in out.stdout


def test_phase7_commands_parse_without_aws():
    for argv in (("rollout", "tick", "--help"), ("traffic", "--help"), ("memory", "purge", "--help"),
                 ("latch", "clear", "--help"), ("flags", "kill", "--help"), ("shadow", "probe", "--help"),
                 ("side-effects", "--help"), ("drift", "check", "--help"), ("drift", "revert", "--help"),
                 ("abtest", "run", "--help"), ("abtest", "show", "--help"), ("recommendation", "import", "--help"),
                 ("model", "compare", "--help"), ("scenario", "add", "--help"), ("corpus", "publish", "--help"),
                 ("cost", "--help"), ("demo", "prep", "--help")):
        out = run(*argv)
        assert out.returncode == 0, (argv, out.stdout, out.stderr)
    assert run("rollout", "tick", "--stage", "qa").returncode == 2


def test_cost_estimate_prints_a_projection_without_aws():
    """The projection comes before any run and needs no credentials."""
    for suite, sessions in (("preview", "24 sessions"), ("staging", "136 sessions"), ("calibration", "126 sessions")):
        out = run("cost", "estimate", "--suite", suite)
        assert out.returncode == 0, out.stdout + out.stderr
        assert sessions in out.stdout and "total $" in out.stdout and "assumption:" in out.stdout
    assert "x 8 runs: 1008 sessions" in run("cost", "estimate", "--suite", "calibration").stdout


def test_gate_and_calibrate_fail_cleanly_without_credentials():
    for args in (("gate", "--stage", "staging", "--profile", "staging"), ("calibrate", "--n", "8"),
                 ("calibration", "show", "--manifest", "2026-10-06T12:58:33Z/b59309b")):
        out = run(*args)
        assert out.returncode == 1, (args, out.stdout, out.stderr)
        assert "Traceback" not in out.stderr and "credentials" in (out.stdout + out.stderr).lower()


def test_baseline_refresh_edits_only_the_baselineManifest_line(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    path = repo / "module11/harness/evaluation/thresholds.yaml"
    before = path.read_text(encoding="utf-8")
    out = run("--root", str(repo), "baseline", "refresh", "--local", "--manifest", "2026-10-06T12:58:33Z/b59309b")
    assert out.returncode == 0, out.stdout + out.stderr
    after = path.read_text(encoding="utf-8")
    assert 'baselineManifest: "2026-10-06T12:58:33Z/b59309b"' in after
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 1 and changed[0][0].startswith("baselineManifest:")
    assert run("--root", str(repo), "baseline", "refresh", "--local", "--manifest",
               "2026-10-06T12:58:33Z/b59309b").returncode == 0


@pytest.mark.parametrize("args", [("release", "deploy", "--stage", "dev"), ("manifest", "diff", "--deployed", "prod"),
                                  ("status", "--all")])
def test_aws_commands_fail_cleanly_without_credentials(args):
    out = run(*args)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "Traceback" not in out.stderr and "credentials" in (out.stdout + out.stderr).lower()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; git with fixed arguments in a temp repo
    subprocess.run([*git, "-C", str(repo), "add", "-A"], check=True)
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; git with fixed arguments in a temp repo
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    return repo, git


def test_detect_against_a_temporary_git_repo(git_repo):
    repo, git = git_repo
    prompt = repo / "module11/harness/agent/prompts/companion.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "Go straight to the logs.\n", encoding="utf-8")
    out = run("--root", str(repo), "detect", "--base", "HEAD")
    assert out.returncode == 0, out.stderr
    plan = json.loads(out.stdout)
    assert plan["kinds"] == ["instructions"] and plan["rollout"] == "canary" and plan["bundleVersion"] is True
    flags = repo / "module11/harness/flags/flags.json"
    doc = json.loads(flags.read_text(encoding="utf-8"))
    doc["flags"]["module10-recovery-planning"]["version"] = 2
    flags.write_text(json.dumps(doc), encoding="utf-8")
    out = run("--root", str(repo), "detect", "--base", "HEAD")
    assert json.loads(out.stdout)["kinds"] == ["flags", "instructions"]
    out = run("--root", str(repo), "ci", "validate", "--base", "HEAD", "--skip-tests")
    assert out.returncode == 1 and "C16" in out.stdout and "land them in their own MR" in out.stdout


@sample_layout_only
def test_seed_dry_run_lists_the_design_layout():
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's seed module with fixed arguments
    out = subprocess.run([sys.executable, "-m", "module11.gitlab.seed", "--dry-run"], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = [l for l in out.stdout.splitlines() if l and not l.endswith(" files")]
    targets = {l.split("  <-  ")[0] for l in lines}
    assert {".gitlab-ci.yml", ".gitlab/CODEOWNERS", "LICENSE", "tests/conftest.py"} <= targets
    assert ".gitlab-ci.yml  <-  module11/gitlab/gitlab-ci.yml" in lines
    allowed = ("module10/", "module11/", "tests/test_module11_", "tests/fixtures/module11/")
    extra = {t for t in targets if not t.startswith(allowed)} - {".gitlab-ci.yml", ".gitlab/CODEOWNERS", "LICENSE",
                                                                   "tests/conftest.py", "demos/module11_demo.py",
                                                                   "vercel.json"}
    from module11.gitlab import seed
    assert extra == set(seed.REUSED_FILES)        # Modules 5, 7 and 9 code that Module 11 reuses unchanged
    assert not any("/.local/" in t or "__pycache__" in t or t.endswith("/venv") for t in targets)


@sample_layout_only
def test_seed_renders_the_group_and_plans_batched_actions():
    from module11.gitlab import seed
    pairs = [(".gitlab/CODEOWNERS", "module11/gitlab/CODEOWNERS"), ("module11/__init__.py", "module11/__init__.py")]
    assert b"@example-group/quality" in seed.content(ROOT, pairs[0][1], pairs[0][0], "example-group")
    read = lambda t: seed.content(ROOT, dict(pairs)[t], t, "example-group")  # noqa: E731
    actions = seed.plan_actions(pairs, {"module11/old.py": "x", "README.md": "y"}, read)
    assert [(a["action"], a["file_path"]) for a in actions] == [
        ("create", ".gitlab/CODEOWNERS"), ("create", "module11/__init__.py"), ("delete", "module11/old.py")]
    assert [len(b) for b in seed.batches(list(range(450)))] == [200, 200, 50]


def test_seed_group_comes_from_configuration_never_a_built_in_default(monkeypatch, capsys):
    """The CODEOWNERS group is the customer's: --group, M11_GITLAB_GROUP, or the group part of --project."""
    from module11.gitlab import seed
    seen = []
    monkeypatch.setattr(seed, "snapshot", lambda root, group: seen.append(group) or [])
    with pytest.raises(SystemExit):
        seed.main(["--target-dir", "/nonexistent"])                  # nothing configured: refuse, no default
    assert "--group" in capsys.readouterr().err and seen == []
    assert seed.main(["--dry-run"]) == 0 and seen.pop() == "<group>"  # a listing renders nothing
    assert seed.main(["--dry-run", "--project", "acme/platform-team/devops-companion-harness"]) == 0
    monkeypatch.setenv("M11_GITLAB_GROUP", "example-group")
    assert seed.main(["--dry-run"]) == 0
    assert seed.main(["--dry-run", "--group", "other-group"]) == 0
    assert seen == ["acme/platform-team", "example-group", "other-group"]


def test_unwired_mr_ui_origin_is_the_customers_team_or_unroutable(monkeypatch):
    from module11.aws import deploy
    assert deploy._unwired_mr_origin("mr-7") == "https://mr-7.ui-preview.invalid"
    monkeypatch.setenv("M11_VERCEL_TEAM_SLUG", "example-team")
    assert deploy._unwired_mr_origin("mr-7") == "https://devops-companion-ui-git-mr-7-example-team.vercel.app"


def test_preview_infra_group_is_read_from_the_project_path(monkeypatch):
    from module11 import cli_preview
    with pytest.raises(cli_preview.PreviewRejected):
        cli_preview._gitlab_group()
    monkeypatch.setenv("M11_GITLAB_PROJECT", "example-group/devops-companion-harness")
    assert cli_preview._gitlab_group() == "example-group"
    monkeypatch.setenv("M11_GITLAB_GROUP", "override-group")
    assert cli_preview._gitlab_group() == "override-group"


def test_isolate_module11_blocks_aws_clients_and_m11_env():
    import boto3
    import module11.aws
    assert not [k for k in os.environ if k.startswith("M11_")]
    for build in (lambda: module11.aws.client("ssm"), lambda: boto3.client("s3"), lambda: boto3.Session().client("sts")):
        with pytest.raises(RuntimeError, match="offline test tried to build an AWS client"):
            build()


def test_flags_audit_writes_one_ledger_entry(monkeypatch):
    """flags kill/restore crashed after the flags write: the audit details repeat `stage` (live on dev)."""
    from types import SimpleNamespace
    from module11 import cli_ops
    from module11.aws import ledger
    calls = []
    monkeypatch.setattr(ledger, "put", lambda s3, bucket, stage, kind, *, by, **fields: calls.append((stage, kind, by, fields)))

    class Admin:
        def client(self, name):
            return SimpleNamespace(get_caller_identity=lambda: {"Arn": "arn:aws:iam::111122223333:user/op"})
    ctx = SimpleNamespace(admin=Admin(), release_bucket="b")
    before = {"flags": {"module10-recovery-planning": {"on": True, "version": 1}}}
    after = {"flags": {"module10-recovery-planning": {"on": True, "version": 2}}}
    cli_ops._flags_audit(ctx, "dev", "kill", "module10-recovery-planning", "payments", before, after)
    assert len(calls) == 1 and calls[0][:3] == ("dev", "flags-kill", "arn:aws:iam::111122223333:user/op")
    assert "stage" not in calls[0][3]


@sample_layout_only
def test_seed_carries_the_reused_closure():
    """The harness repo gets every earlier-module file Module 11 imports (Module 9's closure via corpus/snapshot.py)."""
    from module11.gitlab import seed
    from module11.release import closure as closures
    needed = {f for f in closures.closure("module11/corpus/snapshot.py", ROOT) if not f.startswith("module11/")}
    assert needed and needed <= set(seed.REUSED_FILES)
