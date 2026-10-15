# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The tooling archive names every file the closures list, including edge.txt's `<path> as <name>` lines."""
from pathlib import Path
from module11 import cli_pipeline as CP
from module11.release import closure as closures

ROOT = Path(__file__).resolve().parents[1]


def test_every_edge_closure_path_is_in_the_tooling_archive():
    files = set(CP.tooling_files(ROOT))
    wanted = {path for path, _ in closures.edge_entries(ROOT) if path.startswith("module10/")}
    assert wanted, "edge.txt names no Module 10 files"
    assert wanted <= files
    assert not any(" as " in f for f in files)
