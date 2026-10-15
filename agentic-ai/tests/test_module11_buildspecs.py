# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-image's buildspec: the Image stage passes no DOCKERFILE, so the runtime default must survive into the
build phase (an assignment inside `echo ... | grep` runs in a subshell and is lost)."""
from __future__ import annotations

import os
import subprocess

import pytest
import yaml

pytest.importorskip("pulumi")        # the MR validate image carries no Pulumi
pytest.importorskip("pulumi_aws")
from module11.infrastructure.platform.builds import IMAGE_BUILDSPEC  # noqa: E402


def _dockerfile_after_pre_build(value: str | None) -> subprocess.CompletedProcess:
    lines = [c for c in yaml.safe_load(IMAGE_BUILDSPEC)["phases"]["pre_build"]["commands"] if "DOCKERFILE" in c]
    # CodeBuild runs a phase's commands in one shell and stops at the first failure.
    script = "set -e\n" + "\n".join(lines) + '\nprintf %s "$DOCKERFILE"\n'
    env = {k: v for k, v in os.environ.items() if k != "DOCKERFILE"}
    if value is not None:
        env["DOCKERFILE"] = value
    # nosemgrep: dangerous-subprocess-use-audit - runs the repo's own buildspec lines in bash, as CodeBuild does
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)


def test_the_runtime_dockerfile_is_the_default():
    done = _dockerfile_after_pre_build(None)
    assert done.returncode == 0 and done.stdout == "module11/deployment/Dockerfile"


def test_an_override_is_kept_and_validated():
    done = _dockerfile_after_pre_build("module11/pipeline/build-image/Dockerfile")
    assert done.returncode == 0 and done.stdout == "module11/pipeline/build-image/Dockerfile"
    assert _dockerfile_after_pre_build("../Dockerfile").returncode != 0
