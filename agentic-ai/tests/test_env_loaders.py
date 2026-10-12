# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_env_loaders.py
=========================
Offline tests for the .env loaders used by the Module 9 live check and the
Module 9 corpus seeder.

Each loader prefers python-dotenv (``override=False``) and falls back to a
small parser when python-dotenv is not installed. Both paths must accept
quoted values and ``export`` prefixes, ignore comments, and never override a
value that is already set in the shell.

Run:
    pytest tests/test_env_loaders.py -v
"""

from __future__ import annotations

import builtins
import os

import pytest

import module9.live_check as live_check
import seed_module9_corpora

LOADERS = [
    pytest.param(live_check._load_dotenv, id="module9.live_check"),
    pytest.param(seed_module9_corpora._load_dotenv, id="seed_module9_corpora"),
]

KEYS = (
    "ENVTEST_PLAIN",
    "ENVTEST_DOUBLE",
    "ENVTEST_SINGLE",
    "ENVTEST_EXPORTED",
    "ENVTEST_SHELL_WINS",
    "ENVTEST_COMMENTED",
)

ENV_FILE = """\
# a comment line
ENVTEST_PLAIN=plain-value
ENVTEST_DOUBLE="double quoted value"
ENVTEST_SINGLE='single quoted value'
export ENVTEST_EXPORTED=exported-value
ENVTEST_SHELL_WINS=from-dotenv-file
# ENVTEST_COMMENTED=should-not-load
"""


def _clear_keys(monkeypatch):
    """Remove the test keys now and again at teardown.

    The loaders write os.environ directly, so each key is first set through
    monkeypatch (recording its original state) and then deleted; teardown
    then restores the original state, removing anything a loader added.
    """
    for key in KEYS:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Write a sample .env and clear the keys it uses (restored by monkeypatch)."""
    _clear_keys(monkeypatch)
    path = tmp_path / ".env"
    path.write_text(ENV_FILE, encoding="utf-8")
    return path


@pytest.fixture
def without_dotenv(monkeypatch):
    """Make `from dotenv import load_dotenv` raise ImportError."""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dotenv" or name.startswith("dotenv."):
            raise ImportError("python-dotenv hidden for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _assert_loaded():
    assert os.environ["ENVTEST_PLAIN"] == "plain-value"
    assert os.environ["ENVTEST_DOUBLE"] == "double quoted value"
    assert os.environ["ENVTEST_SINGLE"] == "single quoted value"
    assert os.environ["ENVTEST_EXPORTED"] == "exported-value"
    assert os.environ["ENVTEST_SHELL_WINS"] == "from-shell"
    assert "ENVTEST_COMMENTED" not in os.environ
    assert "# a comment line" not in os.environ


@pytest.mark.parametrize("load", LOADERS)
def test_loader_with_python_dotenv(load, env_file, monkeypatch):
    pytest.importorskip("dotenv")
    monkeypatch.setenv("ENVTEST_SHELL_WINS", "from-shell")
    load(env_file)
    _assert_loaded()


@pytest.mark.parametrize("load", LOADERS)
def test_loader_fallback_parser(load, env_file, monkeypatch, without_dotenv):
    with pytest.raises(ImportError):
        import dotenv  # noqa: F401  (proves the fallback branch is exercised)
    monkeypatch.setenv("ENVTEST_SHELL_WINS", "from-shell")
    load(env_file)
    _assert_loaded()


@pytest.mark.parametrize("load", LOADERS)
def test_loader_missing_file_is_a_no_op(load, tmp_path, monkeypatch):
    _clear_keys(monkeypatch)
    load(tmp_path / "does-not-exist.env")
    assert not any(key in os.environ for key in KEYS)
