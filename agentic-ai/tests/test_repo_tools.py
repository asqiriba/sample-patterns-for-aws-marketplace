"""
tests/test_repo_tools.py
========================
Unit tests for Module 2 repository analysis tools.

Tests all five tools in both mock and real modes.

The tools are LangChain ``StructuredTool`` objects, so tests call them with
``.invoke({...})`` using the tool's argument names.

Run:
    AGENT_MOCK_REPO=true pytest tests/test_repo_tools.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# module2/tools/repo_tools.py reads AGENT_MOCK_REPO at import time, so the
# default must be in place before the module is first imported.
os.environ.setdefault("AGENT_MOCK_REPO", "true")

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_nodejs_repo():
    """Path to sample Node.js repository fixture."""
    return str(Path(__file__).parent / "fixtures" / "sample_repos" / "nodejs-app")


@pytest.fixture
def sample_python_repo():
    """Path to sample Python repository fixture."""
    return str(Path(__file__).parent / "fixtures" / "sample_repos" / "python-app")


@pytest.fixture
def enable_mock_mode():
    """Enable mock mode for tests."""
    original = os.environ.get("AGENT_MOCK_REPO")
    os.environ["AGENT_MOCK_REPO"] = "true"
    yield
    if original is None:
        os.environ.pop("AGENT_MOCK_REPO", None)
    else:
        os.environ["AGENT_MOCK_REPO"] = original


# ---------------------------------------------------------------------------
# Test scan_repository_structure
# ---------------------------------------------------------------------------

def test_scan_repository_structure_mock(enable_mock_mode):
    """Test repository scanning in mock mode."""
    from module2.tools.repo_tools import scan_repository_structure
    
    result = scan_repository_structure.invoke({"repo_path": "/mock/repo/path"})
    data = json.loads(result)
    
    assert data["tool"] == "scan_repository_structure"
    assert data["mock_mode"] is True
    assert "data" in data
    assert "files" in data["data"]
    assert "directories" in data["data"]
    assert len(data["data"]["files"]) > 0


def test_scan_repository_structure_error(monkeypatch):
    """Test scanning non-existent repository."""
    import module2.tools.repo_tools as repo_tools
    from module2.tools.repo_tools import scan_repository_structure

    # The mock flag is read once at import time and the mock branch returns the
    # canned tree for any path, so this real-mode error check switches the
    # module-level flag off for this test only.
    monkeypatch.setattr(repo_tools, "_MOCK", False)
    
    result = scan_repository_structure.invoke({"repo_path": "/nonexistent/path"})
    data = json.loads(result)
    
    assert "error" in data["data"]


# ---------------------------------------------------------------------------
# Test read_file_content
# ---------------------------------------------------------------------------

def test_read_file_content_mock(enable_mock_mode):
    """Test reading file content in mock mode."""
    from module2.tools.repo_tools import read_file_content
    
    result = read_file_content.invoke({"repo_path": "/mock/repo", "file_path": "services/api/package.json"})
    data = json.loads(result)
    
    assert data["tool"] == "read_file_content"
    assert data["mock_mode"] is True
    assert "content" in data["data"]
    assert "express" in data["data"]["content"]


def test_read_file_content_not_found(enable_mock_mode):
    """Test reading non-existent file in mock mode."""
    from module2.tools.repo_tools import read_file_content
    
    result = read_file_content.invoke({"repo_path": "/mock/repo", "file_path": "nonexistent.txt"})
    data = json.loads(result)
    
    assert "error" in data["data"]


# ---------------------------------------------------------------------------
# read_file_content containment and secret-file refusal (real mode)
# ---------------------------------------------------------------------------
# These cases exercise the real filesystem branch, so each one switches the
# import-time mock flag off for that test only (same pattern as
# test_scan_repository_structure_error).

@pytest.fixture
def real_repo(tmp_path, monkeypatch):
    """A tmp_path repo next to a sibling directory that shares its prefix."""
    import module2.tools.repo_tools as repo_tools

    monkeypatch.setattr(repo_tools, "_MOCK", False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    (repo / ".env").write_text("SECRET_TOKEN=do-not-read", encoding="utf-8")
    sibling = tmp_path / "repo-secrets"
    sibling.mkdir()
    (sibling / "f").write_text("sibling-secret", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside-secret", encoding="utf-8")
    return repo


def _read(repo_path, file_path):
    from module2.tools.repo_tools import read_file_content

    return json.loads(read_file_content.invoke({"repo_path": str(repo_path), "file_path": file_path}))


def test_read_file_content_rejects_sibling_prefix_escape(real_repo):
    """/x/repo must not allow /x/repo-secrets/f (string-prefix bug)."""
    data = _read(real_repo, "../repo-secrets/f")
    assert data["mock_mode"] is False
    assert data["data"] == {"error": "File path outside repository"}


def test_read_file_content_rejects_dotdot_traversal(real_repo):
    data = _read(real_repo, "../outside.txt")
    assert data["data"] == {"error": "File path outside repository"}
    assert "outside-secret" not in json.dumps(data)


def test_read_file_content_refuses_env_file(real_repo):
    data = _read(real_repo, ".env")
    assert data["tool"] == "read_file_content"
    assert data["data"] == {"error": "Refusing to read a likely secret file"}
    assert "do-not-read" not in json.dumps(data)


@pytest.mark.parametrize(
    "name",
    [".env.local", "server.pem", "tls.KEY", "cert.p12", "cert.pfx", "id_rsa", "id_ed25519.pub",
     "credentials", ".npmrc", ".pypirc"],
)
def test_read_file_content_refuses_secret_patterns(real_repo, name):
    (real_repo / name).write_text("secret", encoding="utf-8")
    assert _read(real_repo, name)["data"] == {"error": "Refusing to read a likely secret file"}


def test_read_file_content_reads_normal_file(real_repo):
    data = _read(real_repo, "package.json")
    assert data["mock_mode"] is False
    assert data["data"]["file_path"] == "package.json"
    assert data["data"]["content"] == '{"name": "demo"}'
    assert data["data"]["size"] == len('{"name": "demo"}')


# ---------------------------------------------------------------------------
# Test detect_applications
# ---------------------------------------------------------------------------

def test_detect_applications_mock(enable_mock_mode):
    """Test application detection in mock mode."""
    from module2.tools.repo_tools import detect_applications, scan_repository_structure
    
    # First scan the repo
    scan_result = scan_repository_structure.invoke({"repo_path": "/mock/repo"})
    
    # Then detect applications
    result = detect_applications.invoke({"repo_path": "/mock/repo", "file_tree": scan_result})
    data = json.loads(result)
    
    assert data["tool"] == "detect_applications"
    assert data["mock_mode"] is True
    assert "applications" in data["data"]
    assert data["data"]["total_applications"] == 2
    assert any(app["name"] == "api-service" for app in data["data"]["applications"])


# ---------------------------------------------------------------------------
# Test analyze_dependencies
# ---------------------------------------------------------------------------

def test_analyze_dependencies_nodejs_mock(enable_mock_mode):
    """Test dependency analysis for Node.js in mock mode."""
    from module2.tools.repo_tools import analyze_dependencies
    
    result = analyze_dependencies.invoke(
        {"repo_path": "/mock/repo", "app_path": "services/api", "dependency_file": "package.json"}
    )
    data = json.loads(result)
    
    assert data["tool"] == "analyze_dependencies"
    assert "language" in data["data"]
    assert data["data"]["language"] == "Node.js"
    assert data["data"]["framework"] == "Express"
    assert "pg" in data["data"]["dependencies"]
    assert "redis" in data["data"]["dependencies"]


def test_analyze_dependencies_python_mock(enable_mock_mode):
    """Test dependency analysis for Python in mock mode."""
    from module2.tools.repo_tools import analyze_dependencies
    
    result = analyze_dependencies.invoke(
        {"repo_path": "/mock/repo", "app_path": "services/worker", "dependency_file": "requirements.txt"}
    )
    data = json.loads(result)
    
    assert data["tool"] == "analyze_dependencies"
    assert "language" in data["data"]
    assert data["data"]["language"] == "Python"
    assert data["data"]["framework"] == "FastAPI"
    assert "celery" in data["data"]["dependencies"]


# ---------------------------------------------------------------------------
# Test map_aws_services
# ---------------------------------------------------------------------------

def test_map_aws_services_mock(enable_mock_mode):
    """Test AWS service mapping in mock mode."""
    from module2.tools.repo_tools import analyze_dependencies, map_aws_services
    
    # First analyze dependencies
    deps_result = analyze_dependencies.invoke(
        {"repo_path": "/mock/repo", "app_path": "services/api", "dependency_file": "package.json"}
    )
    
    # Then map to AWS services
    result = map_aws_services.invoke({"dependencies": deps_result})
    data = json.loads(result)
    
    assert data["tool"] == "map_aws_services"
    assert "aws_services" in data["data"]
    assert "database" in data["data"]["aws_services"]
    assert "cache" in data["data"]["aws_services"]
    assert "storage" in data["data"]["aws_services"]
    assert "compute" in data["data"]["aws_services"]
    
    # Check specific mappings
    db_services = data["data"]["aws_services"]["database"]
    assert any(s["service"] == "RDS" and s["engine"] == "PostgreSQL" for s in db_services)
    
    cache_services = data["data"]["aws_services"]["cache"]
    assert any(s["service"] == "ElastiCache" and s["engine"] == "Redis" for s in cache_services)


def test_map_aws_services_summary(enable_mock_mode):
    """Test AWS service mapping summary."""
    from module2.tools.repo_tools import analyze_dependencies, map_aws_services
    
    deps_result = analyze_dependencies.invoke(
        {"repo_path": "/mock/repo", "app_path": "services/api", "dependency_file": "package.json"}
    )
    result = map_aws_services.invoke({"dependencies": deps_result})
    data = json.loads(result)
    
    assert "summary" in data["data"]
    summary = data["data"]["summary"]
    assert "databases" in summary
    assert "caching" in summary
    assert "storage" in summary
    assert "compute" in summary
    assert "RDS" in summary["databases"]
    assert "ElastiCache" in summary["caching"]


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

def test_full_analysis_workflow_mock(enable_mock_mode):
    """Test complete analysis workflow in mock mode."""
    from module2.tools.repo_tools import (
        analyze_dependencies,
        detect_applications,
        map_aws_services,
        scan_repository_structure,
    )
    
    repo_path = "/mock/repo"
    
    # Step 1: Scan
    scan_result = scan_repository_structure.invoke({"repo_path": repo_path})
    scan_data = json.loads(scan_result)
    assert "files" in scan_data["data"]
    
    # Step 2: Detect applications
    detect_result = detect_applications.invoke({"repo_path": repo_path, "file_tree": scan_result})
    detect_data = json.loads(detect_result)
    assert detect_data["data"]["total_applications"] > 0
    
    # Step 3: Analyze dependencies for first app
    apps = detect_data["data"]["applications"]
    first_app = apps[0]
    
    # Find dependency file
    dep_file = "package.json" if "package.json" in first_app["indicators"] else "requirements.txt"
    
    deps_result = analyze_dependencies.invoke(
        {"repo_path": repo_path, "app_path": first_app["path"], "dependency_file": dep_file}
    )
    deps_data = json.loads(deps_result)
    assert "dependencies" in deps_data["data"]
    
    # Step 4: Map to AWS services
    aws_result = map_aws_services.invoke({"dependencies": deps_result})
    aws_data = json.loads(aws_result)
    assert "aws_services" in aws_data["data"]
    assert len(aws_data["data"]["aws_services"]) > 0


# ---------------------------------------------------------------------------
# Dependency Mapping Tests
# ---------------------------------------------------------------------------

def test_dependency_mapping_coverage():
    """Test that common dependencies are mapped correctly."""
    from module2.tools.repo_tools import DEPENDENCY_TO_AWS_SERVICE
    
    # Database libraries
    assert "pg" in DEPENDENCY_TO_AWS_SERVICE
    assert DEPENDENCY_TO_AWS_SERVICE["pg"]["service"] == "RDS"
    assert DEPENDENCY_TO_AWS_SERVICE["pg"]["engine"] == "PostgreSQL"
    
    assert "mysql" in DEPENDENCY_TO_AWS_SERVICE
    assert DEPENDENCY_TO_AWS_SERVICE["mysql"]["service"] == "RDS"
    
    assert "mongodb" in DEPENDENCY_TO_AWS_SERVICE
    assert DEPENDENCY_TO_AWS_SERVICE["mongodb"]["service"] == "DocumentDB"
    
    # Cache libraries
    assert "redis" in DEPENDENCY_TO_AWS_SERVICE
    assert DEPENDENCY_TO_AWS_SERVICE["redis"]["service"] == "ElastiCache"
    
    # Storage libraries
    assert "boto3" in DEPENDENCY_TO_AWS_SERVICE
    assert DEPENDENCY_TO_AWS_SERVICE["boto3"]["service"] == "S3"
    
    # Framework libraries
    assert "express" in DEPENDENCY_TO_AWS_SERVICE
    assert "fastapi" in DEPENDENCY_TO_AWS_SERVICE
    assert "flask" in DEPENDENCY_TO_AWS_SERVICE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
