"""Image builds must succeed before a deployable digest is reported."""
import ast
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module10.deployment import build_image


ROOT = Path(__file__).resolve().parents[1]


class ImageNotFound(Exception):
    pass


def ecr_client():
    client = Mock()
    client.exceptions = SimpleNamespace(ImageNotFoundException=ImageNotFound)
    return client


def test_waits_for_success(monkeypatch):
    client = Mock()
    client.batch_get_builds.side_effect = [
        {"builds": [{"buildStatus": "IN_PROGRESS"}]},
        {"builds": [{"buildStatus": "SUCCEEDED"}]},
    ]
    monkeypatch.setattr(build_image.time, "sleep", Mock())
    build_image.wait_for_build(client, "test-build")
    assert client.batch_get_builds.call_count == 2


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"])
def test_failed_build_cannot_produce_deployable_report(status):
    client = Mock()
    client.batch_get_builds.return_value = {"builds": [{"buildStatus": status}]}
    with pytest.raises(RuntimeError, match=status):
        build_image.wait_for_build(client, "test-build")


def test_wait_has_a_deadline(monkeypatch):
    monkeypatch.setattr(build_image.time, "monotonic", Mock(side_effect=[0, 2]))
    with pytest.raises(TimeoutError, match="test-build"):
        build_image.wait_for_build(Mock(), "test-build", timeout=1)


@pytest.mark.parametrize("existing", [True, False])
def test_build_reports_digest_and_reuses_immutable_image(tmp_path, monkeypatch, existing):
    outputs = tmp_path / "outputs.json"
    outputs.write_text(json.dumps({
        "repositoryName": "test-repo", "artifactBucket": "test-artifacts", "buildProject": "test-project",
    }))
    report = tmp_path / "image.json"
    ecr = ecr_client()
    image = {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}
    ecr.describe_images.side_effect = [image] if existing else [ImageNotFound(), image]
    builder = Mock()
    builder.start_build.return_value = {"build": {"id": "test-build"}}
    builder.batch_get_builds.return_value = {"builds": [{"buildStatus": "SUCCEEDED"}]}
    s3 = Mock()
    monkeypatch.setattr(build_image.boto3, "client",
                        lambda name: {"s3": s3, "codebuild": builder, "ecr": ecr}[name])
    monkeypatch.setattr(build_image, "source_archive", lambda root: b"test-source")
    monkeypatch.setattr(build_image.sys, "argv", [
        "build_image", "--outputs", str(outputs), "--wait", "--report", str(report),
    ])
    build_image.main()
    assert json.loads(report.read_text())["image_digest"] == "sha256:" + "a" * 64
    assert builder.start_build.call_count == (0 if existing else 1)
    assert s3.put_object.call_count == (0 if existing else 1)


def test_registry_permission_errors_are_not_treated_as_missing_images():
    ecr = ecr_client()
    ecr.describe_images.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        build_image.image_digest(ecr, "test-repo", "tag")


def test_portkey_build_refreshes_and_patches_base_image():
    dockerfile = (ROOT / "module10/gateway/Dockerfile").read_text()
    infrastructure = (ROOT / "module10/gateway/pulumi/__main__.py").read_text()
    assert dockerfile.count("apt-get upgrade -y") == 2
    assert "docker build --pull --platform linux/arm64" in infrastructure


def _lifecycle_calls():
    program = ast.parse((ROOT / "module10/deployment/pulumi/__main__.py").read_text())
    return [
        node for node in ast.walk(program)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "BucketLifecycleConfigurationV2"
    ]


def test_artifact_retention_expires_results_and_manifests():
    calls = _lifecycle_calls()
    # One resource with the same logical name keeps this an in-place update on live stacks.
    assert len(calls) == 1
    call = calls[0]
    assert [ast.literal_eval(arg) for arg in call.args] == ["artifacts-retention"]
    rules = ast.literal_eval(next(kw.value for kw in call.keywords if kw.arg == "rules"))
    assert len(rules) == 2
    assert rules[0] == {
        "id": "demo-results", "status": "Enabled", "filter": {"prefix": "results/"},
        "expiration": {"days": 7}, "noncurrent_version_expiration": {"noncurrent_days": 7},
    }
    assert rules[1] == {
        "id": "demo-manifests", "status": "Enabled", "filter": {"prefix": "manifests/"},
        "expiration": {"days": 30}, "noncurrent_version_expiration": {"noncurrent_days": 30},
    }


@pytest.mark.parametrize("name", ["edge.py", "ui.py"])
def test_hashed_deployment_programs_match_main(name):
    # edge.py hashes its own source into the API Gateway deployment trigger, and
    # ui.py hashes the site, so any edit to them redeploys live resources.
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; git show of a fixed path on main
    shown = subprocess.run(
        [git, "-C", str(ROOT), "show", f"main:agentic-ai/module10/deployment/pulumi/{name}"],
        capture_output=True, check=False,
    )
    if shown.returncode != 0:
        pytest.skip("no local main branch to compare against")
    current = (ROOT / "module10/deployment/pulumi" / name).read_bytes()
    assert hashlib.sha256(current).hexdigest() == hashlib.sha256(shown.stdout).hexdigest()
