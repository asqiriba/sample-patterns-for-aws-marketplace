# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared offline helpers for the Module 11 runtime tests (no tests here). The name keeps it
inside the `tests/test_module11_*` set that seed.py copies to the GitLab project."""
from __future__ import annotations

import atexit
import copy
import json
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from module11.cli import setup_local_runtime
from module11.runtime.app import Runtime
from module11.runtime.config import parse_runtime_config
from module11.runtime.fake_model import FakeModelClient
from module11.runtime.telemetry import RecordingBackend

ROOT = Path(__file__).resolve().parents[1]
# The sample repository keeps the GitLab files under module11/gitlab/; the seeded harness repository holds their
# rendered copies (.gitlab-ci.yml, .gitlab/CODEOWNERS), so tests of the source files run only in the sample.
SAMPLE_LAYOUT = (ROOT / "module11/gitlab/CODEOWNERS").is_file()
sample_layout_only = __import__("pytest").mark.skipif(
    not SAMPLE_LAYOUT, reason="tests the sample repository's module11/gitlab/ sources (the harness repo holds renders)")
FLAG_KEY = "module10-recovery-planning"


@lru_cache(maxsize=1)
def _template() -> Path:
    out = Path(tempfile.mkdtemp(prefix="m11-runtime-template-")).resolve()
    atexit.register(shutil.rmtree, out, True)
    setup_local_runtime(ROOT, out)
    return out


def local_dir(tmp_path: Path) -> Path:
    """A fresh copy of the `runtime setup-local` directory, with configs rewritten to it."""
    src = _template()
    dst = (tmp_path / "runtime").resolve()
    shutil.copytree(src, dst)
    for name in ("config.json", "config-bootstrap.json"):
        path = dst / name
        path.write_text(path.read_text(encoding="utf-8").replace(src.as_uri(), dst.as_uri()), encoding="utf-8")
    return dst


def config_doc(directory: Path, name: str = "config.json") -> dict:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def envelope(directory: Path, name: str = "envelope-investigation.json", **context) -> dict:
    doc = copy.deepcopy(json.loads((directory / name).read_text(encoding="utf-8")))
    doc["context"].update(context)
    return doc


class FakeFlags:
    def __init__(self, planning: bool = True) -> None:
        self.planning = planning

    def evaluate(self, team: str):
        release = {"flag_key": FLAG_KEY, "recovery_planning_enabled": self.planning, "reason": {"kind": "TARGET_MATCH"},
                   "version": 1, "variation_index": 1 if self.planning else 0, "context_kind": "team", "context_key": team}
        return release, "sha256:" + "f" * 64, {FLAG_KEY: self.planning}

    def close(self) -> None:
        pass


def make_runtime(directory: Path, *, planning: bool = True, model=None, guardrail=None, invoker=None,
                 backend=None, flags=None, config: dict | None = None, bundles=None) -> Runtime:
    cfg = parse_runtime_config(json.dumps(config or config_doc(directory)))
    return Runtime(cfg, local=True, model_client=model or FakeModelClient(), guardrail=guardrail, invoker=invoker,
                   backend=backend or RecordingBackend(), flags=flags or FakeFlags(planning), bundles=bundles)


def invoke(runtime: Runtime, payload: dict, headers: dict | None = None):
    events = []
    invocation, bundle, override = runtime.prepare(payload, headers or {})
    body = runtime.investigate(invocation, bundle, override, headers or {}, emit=lambda n, d: events.append((n, d)))
    return body, events


def stored_result(directory: Path, body: dict) -> dict:
    path = directory / "bucket" / "results" / body["team"] / body["user_id"] / f"{body['request_id']}.json"
    return json.loads(path.read_text(encoding="utf-8"))
