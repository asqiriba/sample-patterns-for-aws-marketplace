# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The v2 demo driver offline: every committed capture renders under the
REPLAY banner with --replay-fixtures and with --replay; --section, --cut and --no-pause work; the cut levels
skip exactly C1-C5 and C1-C8 (17:15 / 15:15 / 11:55); `d 0 --no-pause` runs under zsh -c and bash -c with
M11_CUT unset, empty, 15 and 12; every `d N`, cut ID and key on the presenter cheat sheet exists in SECTIONS."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "demos/module11_demo.py"
FIXTURES = ROOT / "tests/fixtures/module11/captures"
SHEET = ROOT / "module11/docs/PRESENTER_CHEAT_SHEET.md"
D_FUNCTION = ('d() { local n="$1"; shift; "$VENV/bin/python" demos/module11_demo.py --section "$n" '
              '--cut="${M11_CUT:-full}" "$@"; }')


def driver():
    if "module11_demo" in sys.modules:
        return sys.modules["module11_demo"]
    spec = importlib.util.spec_from_file_location("module11_demo", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["module11_demo"] = module          # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


def bare_env(tmp_path, **extra):
    """No AWS credentials and no profile: the newcomer path."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "M11_TIMINGS_DIR": str(tmp_path / "timings"), **extra}


def run_driver(tmp_path, *args, env=None):
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; the demo driver with the test's fixed arguments
    return subprocess.run([sys.executable, str(DRIVER), *args], cwd=ROOT, capture_output=True, text=True,
                          env=env or bare_env(tmp_path), timeout=120)


def test_budgets_and_cut_levels():
    m = driver()
    totals = {cut: sum(m.budget(n, cut) for n in m.SECTIONS) for cut in m.CUT_LEVELS}
    assert totals == {"full": 17 * 60 + 15, "15": 15 * 60 + 15, "12": 11 * 60 + 55}
    assert m.skipped("15") == {"6d", "6e", "4d", "3c", "1c"}
    assert m.skipped("12") == m.skipped("15") | {"2d", "2e", "6b", "6c", "4a", "4b", "4c"}
    assert sorted(m.SECTIONS) == list(range(8)) and set(m.TITLES) == set(m.SECTIONS)
    budgets = {s.id: s.budget for subs in m.SUBBEATS.values() for s in subs}
    assert (budgets["1a"], budgets["1b"], budgets["1c"], budgets["1d"], budgets["1e"]) == (20, 30, 35, 10, 25)
    assert (budgets["3d"], budgets["3e"], budgets["3f"]) == (40, 60, 10) and budgets["F4"] == 45
    cut_of = {s.id: s.cut for subs in m.SUBBEATS.values() for s in subs}
    assert cut_of["6d"] == "C1" and cut_of["6e"] == "C2" and cut_of["4d"] == "C3" and cut_of["3c"] == "C4"
    assert cut_of["1c"] == "C5" and cut_of["2d"] == cut_of["2e"] == "C6" and cut_of["6b"] == cut_of["6c"] == "C7"
    assert cut_of["4a"] == cut_of["4b"] == cut_of["4c"] == "C8"
    assert not any(s.cut for s in m.SUBBEATS[5]) and cut_of["1e"] is None and cut_of["6a"] is None


@pytest.mark.parametrize("cut, budget", [("full", "17:15"), ("15", "15:15"), ("12", "11:55")])
def test_replay_fixtures_render_every_committed_capture_under_the_banner(tmp_path, cut, budget):
    result = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--cut", cut)
    assert result.returncode == 0, result.stderr
    assert f"cut level: {cut}" in result.stdout and f"budget {budget}" in result.stdout
    committed = sorted(p.stem for p in FIXTURES.glob("*.json")) if FIXTURES.is_dir() else []
    m = driver()
    for name in committed:
        doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        assert doc["schema"] == "m11-capture/1" and doc.get("redacted") is True
        if name == "b5-canary" and (FIXTURES / "b5.json").exists():
            continue
        assert "REPLAY of real run" in result.stdout
        gone = m.skipped(cut)
        texts = [e["text"] for e in doc["events"] if e.get("sub") not in gone and e.get("text")
                 and e.get("kind") not in ("header", "banner")]
        for text in texts[:5]:
            assert text in result.stdout
        for sub in sorted({e.get("sub") for e in doc["events"]} & gone):
            assert f"[{sub}] skipped: cut level {cut}" in result.stdout
    for n in range(8):
        name = "b5-canary" if n == 5 and "b5" not in committed else f"b{n}"
        if name not in committed:
            assert f"no capture {name}.json" in result.stdout
    assert list((tmp_path / "timings").glob("*.json"))          # every run writes its timings JSON


def test_section_and_replay_of_one_capture(tmp_path):
    result = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--section", "7")
    assert result.returncode == 0 and "B7 Wrap" in result.stdout and "B6" not in result.stdout
    for name in sorted(p.stem for p in FIXTURES.glob("*.json")) if FIXTURES.is_dir() else []:
        out = run_driver(tmp_path, "--replay", str(FIXTURES / f"{name}.json"), "--no-pause")
        assert out.returncode == 0 and "REPLAY of real run" in out.stdout


def test_cut_rejects_anything_else(tmp_path):
    assert run_driver(tmp_path, "--replay-fixtures", "--cut", "10").returncode == 2
    bad = run_driver(tmp_path, "--replay-fixtures", "--no-pause", env=bare_env(tmp_path, M11_CUT="13"))
    assert bad.returncode == 2 and "M11_CUT" in bad.stderr


@pytest.mark.parametrize("shell", ["zsh", "bash"])
@pytest.mark.parametrize("value, expected", [(None, "full"), ("", "full"), ("15", "15"), ("12", "12")])
def test_d_function_under_zsh_and_bash(tmp_path, shell, value, expected):
    exe = shutil.which(shell, path="/bin:/usr/bin")
    if exe is None:
        pytest.skip(f"{shell} not installed")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    env = bare_env(tmp_path, VENV=str(venv))
    prefix = "unset M11_CUT; " if value is None else f"export M11_CUT='{value}'; "
    # nosemgrep: dangerous-subprocess-use-audit - a system shell running the documented d() helper with a fixed value
    result = subprocess.run([exe, "-c", f"{D_FUNCTION}\n{prefix}d 0 --no-pause --replay-fixtures"], cwd=ROOT,
                            capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    assert f"cut level: {expected} " in result.stdout
    assert "B0 Context" in result.stdout and "B1" not in result.stdout


def test_cheat_sheet_matches_the_driver():
    m = driver()
    text = SHEET.read_text(encoding="utf-8")
    sections = {int(n) for n in re.findall(r"`d (\d+)`", text)}
    assert sections == set(m.SECTIONS)
    cuts = set(re.findall(r"\bC(\d)\b", text))
    assert {f"C{c}" for c in cuts} <= set(m.CUTS) and len(cuts) == 8
    for sub, cut in re.findall(r"\b(\d[a-f](?:-\d[a-f])?) = (C\d)", text):
        first = sub.split("-")[0]
        assert first in m.CUTS[cut], (sub, cut)
    for key in re.findall(r"\*\*(Enter|[a-z])\*\*", text):
        assert key in m.KEYS, key
    assert "17:15" in text and "15:15" in text and "11:55" in text


def test_d_function_is_the_one_demo_prep_writes(tmp_path):
    from module11.demo import prep
    env = tmp_path / "env.sh"
    env.write_text("export VENV=/x\n", encoding="utf-8")
    assert prep.ensure_d_function(tmp_path, env) is True
    assert prep.ensure_d_function(tmp_path, env) is False                 # idempotent
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines.count(D_FUNCTION) == 1 and prep.D_FUNCTION == D_FUNCTION


def test_reset_helpers():
    from module11.demo import prep
    mrs = [{"iid": 1, "source_branch": "friday-brevity", "labels": []},
           {"iid": 2, "source_branch": "friday-brevity-fallback", "labels": []},
           {"iid": 3, "source_branch": "scenario/x", "labels": ["m11-demo-run"]},
           {"iid": 4, "source_branch": "demo/payments-v20", "labels": []}]
    friday, demo_run = prep.mrs_to_close(mrs, fallback_iid=2)
    assert [m["iid"] for m in friday] == [1] and [m["iid"] for m in demo_run] == [3]
    files, deleted = prep.revert_files(["a", "b"], lambda p: b"x" if p == "a" else None)
    assert files == {"a": b"x"} and deleted == ["b"]
    assert prep.next_number(["harness/v18", "harness/v9", "other"]) == 19


def test_no_credentials_needed(tmp_path):
    env = bare_env(tmp_path)
    assert not any(k.startswith("AWS_") for k in env)
    assert run_driver(tmp_path, "--replay-fixtures", "--no-pause", env=env).returncode == 0
    _ = os.environ
def test_b2_scratch_copy_keeps_symlinks_instead_of_copying_the_virtual_environments(tmp_path):
    """2b took 159 s in the capture (budget 15 s): copytree followed the Pulumi projects' `venv` symlinks and copied
    about 56,000 files. The scratch copy keeps them as links and skips .local and __pycache__."""
    m = driver()
    src, outside = tmp_path / "module11", tmp_path / "big-venv"
    (src / "infrastructure/platform").mkdir(parents=True)
    (src / ".local").mkdir()
    (src / "__pycache__").mkdir()
    (src / "agent.md").write_text("keep", encoding="utf-8")
    (src / ".local/state.json").write_text("{}", encoding="utf-8")
    outside.mkdir()
    (outside / "heavy.bin").write_text("x", encoding="utf-8")
    (src / "infrastructure/platform/venv").symlink_to(outside)
    dest = tmp_path / "scratch/module11"
    m._scratch_copy(src, dest)
    link = dest / "infrastructure/platform/venv"
    assert link.is_symlink() and link.resolve() == outside.resolve()
    assert (dest / "agent.md").read_text(encoding="utf-8") == "keep"
    assert not (dest / ".local").exists() and not (dest / "__pycache__").exists()
@pytest.mark.parametrize("no_pause", [True, False])
def test_b2_revert_moves_to_the_background_after_its_foreground_budget_with_or_without_no_pause(tmp_path, monkeypatch,
                                                                                              no_pause):
    """2e backgrounds a revert that is still running when REVERT_FOREGROUND_S passes. The wait is wall-clock, so the
    Enter-paced and the --no-pause paths behave the same."""
    from types import SimpleNamespace
    from module11.demo import race
    m = driver()
    events, said = [], []
    run = SimpleNamespace(args=SimpleNamespace(no_pause=no_pause), notes={}, event=lambda name, **f: events.append(name),
                          screen=SimpleNamespace(say=lambda *a, **k: said.append(a[0])))

    class Child:
        pid = 4242
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "LOCAL", tmp_path / ".local")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(race, "REVERT_FOREGROUND_S", 0.2)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(race, "write_drift_revert", lambda **k: None)
    background = []
    monkeypatch.setattr(race, "set_backgrounded", lambda *a, **k: background.append(True))
    m._drift_revert(run)
    assert events == ["drift revert backgrounded"] and background == [True]
    assert any("continuing in background" in line for line in said)
