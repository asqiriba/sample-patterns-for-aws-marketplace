# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/closure.py, including the runtime/app.py
closure that is the image's build context."""
import io
import zipfile
from pathlib import Path

from module11.release import artifacts as A
from module11.release import closure as C
from module11.release.check import c12

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lazy_import_inside_a_function_is_captured(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py", "import json\n\ndef f():\n    from .lazy import value\n    return value\n")
    _write(tmp_path, "pkg/lazy.py", "value = 1\n")
    _write(tmp_path, "pkg/unused.py", "x = 2\n")
    _write(tmp_path, "entry.py", "from pkg.core import f\nprint(f())\n")
    got = C.closure("entry.py", tmp_path)
    assert got == ["entry.py", "pkg/__init__.py", "pkg/core.py", "pkg/lazy.py"]


def test_nested_package_inits_are_included(tmp_path):
    _write(tmp_path, "a/__init__.py", "")
    _write(tmp_path, "a/b/__init__.py", "")
    _write(tmp_path, "a/b/c.py", "")
    _write(tmp_path, "entry.py", "import a.b.c\n")
    assert C.closure(tmp_path / "entry.py", tmp_path) == ["a/__init__.py", "a/b/__init__.py", "a/b/c.py", "entry.py"]


def test_missing_entry_is_empty(tmp_path):
    assert C.closure("nope.py", tmp_path) == []


def test_handler_closure_is_handler_files_plus_repo_imports(tmp_path):
    _write(tmp_path, f"{C.TOOLS_DIR}/t1/handler/index.py", "import logic\n")
    _write(tmp_path, f"{C.TOOLS_DIR}/t1/handler/logic.py", "def run(x, data):\n    from shared.util import y\n")
    _write(tmp_path, f"{C.TOOLS_DIR}/t1/handler/data.txt", "extra\n")
    _write(tmp_path, "shared/__init__.py", "")
    _write(tmp_path, "shared/util.py", "y = 1\n")
    got = C.handler_closure("t1", tmp_path)
    assert got == ["module11/harness/agent/tools/t1/handler/data.txt", "module11/harness/agent/tools/t1/handler/index.py",
                   "module11/harness/agent/tools/t1/handler/logic.py", "shared/__init__.py", "shared/util.py"]


def test_handler_zip_layout_and_determinism(tmp_path):
    _write(tmp_path, f"{C.TOOLS_DIR}/t1/handler/index.py", "import logic\n")
    _write(tmp_path, f"{C.TOOLS_DIR}/t1/handler/logic.py", "def run(x, data):\n    from shared.util import y\n")
    _write(tmp_path, "shared/__init__.py", "")
    _write(tmp_path, "shared/util.py", "y = 1\n")
    first = C.handler_zip("t1", tmp_path)
    assert C.handler_zip("t1", tmp_path) == first
    names = zipfile.ZipFile(io.BytesIO(first)).namelist()
    assert names == ["index.py", "logic.py", "shared/__init__.py", "shared/util.py"]
    assert {i.date_time for i in zipfile.ZipFile(io.BytesIO(first)).infolist()} == {C.FIXED_TIME}


def test_committed_handler_closures_equal_the_handler_artifact_inputs():
    aset = A.compute(ROOT)
    for tool in C.lambda_tools(ROOT):
        listed = C.read_list(ROOT, f"handler-{tool}.txt")
        assert listed == C.handler_closure(tool, ROOT)
        assert set(aset.artifacts[f"tool/{tool}/handler"].sources) == set(listed) | {A.LAMBDA_REQUIREMENTS}


def test_runtime_closure_contains_module10_and_every_runtime_module():
    got = C.closure(ROOT / C.RUNTIME_ENTRY, ROOT)
    for rel in ("module10/__init__.py", "module10/exposure.py", "module10/agent.py", "module10/control_planes.py",
                "module10/recovery.py", "module10/agent_runner.py", "module10/reviewer.py", "module10/flags.py",
                "module11/__init__.py", "module11/aws/__init__.py", "module11/aws/objects.py",
                "module11/runtime/__init__.py", C.RUNTIME_ENTRY):
        assert rel in got, rel
    runtime_modules = {p.relative_to(ROOT).as_posix() for p in (ROOT / "module11/runtime").glob("*.py")}
    assert runtime_modules <= set(got)  # lazy imports (fake_model, openai paths) are captured too
    # Nothing from the release engines, the CLI or infrastructure ships in the image.
    assert not [p for p in got if p.startswith(("module11/release/", "module11/infrastructure/", "module11/cli.py"))]
    assert C.read_list(ROOT, "runtime.txt") == got
    aset = A.compute(ROOT)
    assert set(got) <= set(aset.artifacts["runtime/code"].sources)


def test_stale_committed_list_fails_c12(tmp_path):
    import shutil
    root = tmp_path / "h"
    shutil.copytree(FIXTURE, root)
    assert c12(root) == []
    logic = root / C.TOOLS_DIR / "query_metrics/handler/helper.py"
    logic.write_text("X = 1\n", encoding="utf-8")
    findings = c12(root)
    assert [f.path for f in findings] == [f"{C.CLOSURES_DIR}/handler-query_metrics.txt"]
    assert "closure stale: run `module11 closure update`" in findings[0].message
    assert C.update(root) == ["handler-query_metrics.txt"]
    assert c12(root) == []


def test_edge_list_imports_must_resolve(tmp_path):
    _write(tmp_path, "module11/edge/lambda/adapter.mjs", 'import { a } from "./m10adapter.mjs";\nimport { r } from "./router.mjs";\n')
    _write(tmp_path, "module11/edge/lambda/router.mjs", "export const r = 1;\n")
    _write(tmp_path, "module10/deployment/lambda/adapter.mjs", "export const a = 1;\n")
    _write(tmp_path, f"{C.CLOSURES_DIR}/edge.txt",
           C.EDGE_HEADER + "module11/edge/lambda/adapter.mjs\nmodule11/edge/lambda/router.mjs\n")
    assert C.edge_problems(tmp_path) == ["module11/edge/lambda/adapter.mjs imports ./m10adapter.mjs, which edge.txt does not list"]
    _write(tmp_path, f"{C.CLOSURES_DIR}/edge.txt", C.EDGE_HEADER + "module11/edge/lambda/adapter.mjs\n"
           "module11/edge/lambda/router.mjs\nmodule10/deployment/lambda/adapter.mjs as m10adapter.mjs\nmissing.mjs\n")
    assert C.edge_problems(tmp_path) == ["edge.txt lists a missing file: missing.mjs"]
