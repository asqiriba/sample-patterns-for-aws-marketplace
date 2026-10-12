"""
module2/tools/repo_tools.py
===========================
Repository analysis tools for Module 2 Repository Analysis Agent.

These tools enable the agent to analyze local git repositories to identify
applications, technology stacks, and AWS infrastructure requirements.

DESIGN PRINCIPLES
-----------------
- Local git repository analysis only (no remote APIs in Module 2)
- Read-only operations (no repository modifications)
- Structured JSON output for LLM consumption
- Mock mode support for testing without real repositories

TOOLS
-----
1. scan_repository_structure  - List files/directories with git awareness
2. read_file_content          - Read specific files (package.json, etc.)
3. detect_applications        - Identify distinct apps/services
4. analyze_dependencies       - Parse dependency files
5. map_aws_services          - Map dependencies to AWS services
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

# Mock mode flag
_MOCK = os.getenv("AGENT_MOCK_REPO", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Dependency to AWS Service Mapping
# ---------------------------------------------------------------------------

DEPENDENCY_TO_AWS_SERVICE = {
    # Databases
    "pg": {"service": "RDS", "engine": "PostgreSQL", "category": "database"},
    "postgres": {"service": "RDS", "engine": "PostgreSQL", "category": "database"},
    "psycopg2": {"service": "RDS", "engine": "PostgreSQL", "category": "database"},
    "mysql": {"service": "RDS", "engine": "MySQL", "category": "database"},
    "mysql2": {"service": "RDS", "engine": "MySQL", "category": "database"},
    "pymysql": {"service": "RDS", "engine": "MySQL", "category": "database"},
    "mongodb": {"service": "DocumentDB", "engine": "MongoDB", "category": "database"},
    "pymongo": {"service": "DocumentDB", "engine": "MongoDB", "category": "database"},
    
    # Caching
    "redis": {"service": "ElastiCache", "engine": "Redis", "category": "cache"},
    "ioredis": {"service": "ElastiCache", "engine": "Redis", "category": "cache"},
    "memcached": {"service": "ElastiCache", "engine": "Memcached", "category": "cache"},
    
    # Storage
    "aws-sdk": {"service": "S3", "engine": "Object Storage", "category": "storage"},
    "boto3": {"service": "S3", "engine": "Object Storage", "category": "storage"},
    "@aws-sdk/client-s3": {"service": "S3", "engine": "Object Storage", "category": "storage"},
    
    # Queuing
    "amqplib": {"service": "Amazon MQ", "engine": "RabbitMQ", "category": "queue"},
    "pika": {"service": "Amazon MQ", "engine": "RabbitMQ", "category": "queue"},
    "celery": {"service": "SQS", "engine": "Message Queue", "category": "queue"},
    
    # Search
    "elasticsearch": {"service": "OpenSearch", "engine": "Elasticsearch", "category": "search"},
    "opensearch-py": {"service": "OpenSearch", "engine": "OpenSearch", "category": "search"},
    
    # Web frameworks (compute inference)
    "express": {"service": "ECS/Lambda", "engine": "Node.js", "category": "compute"},
    "fastapi": {"service": "ECS/Lambda", "engine": "Python", "category": "compute"},
    "flask": {"service": "ECS/Lambda", "engine": "Python", "category": "compute"},
    "django": {"service": "ECS/Lambda", "engine": "Python", "category": "compute"},
    "gin": {"service": "ECS/Lambda", "engine": "Go", "category": "compute"},
    "spring-boot": {"service": "ECS/Lambda", "engine": "Java", "category": "compute"},
}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _wrap(data: Any, tool_name: str) -> str:
    """Wrap tool output in consistent JSON envelope."""
    return json.dumps(
        {
            "tool": tool_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mock_mode": _MOCK,
            "data": data,
        },
        indent=2,
        default=str,
    )


def _is_dependency_file(filename: str) -> bool:
    """Check if file is a dependency manifest."""
    dependency_files = {
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "requirements.txt", "Pipfile", "Pipfile.lock", "poetry.lock", "pyproject.toml",
        "go.mod", "go.sum",
        "Gemfile", "Gemfile.lock",
        "pom.xml", "build.gradle", "build.gradle.kts",
        "Cargo.toml", "Cargo.lock",
    }
    return filename in dependency_files


def _is_config_file(filename: str) -> bool:
    """Check if file is a configuration file."""
    config_files = {
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".dockerignore", "Makefile",
        "serverless.yml", "serverless.yaml",
        "terraform.tf", "main.tf", "variables.tf",
        "cloudformation.yaml", "cloudformation.yml", "template.yaml",
        "cdk.json", "app.py", "stack.py",
    }
    return filename in config_files or filename.endswith((".tf", ".tfvars"))


# File names read_file_content refuses to return, because they usually hold
# credentials or keys (matched case-insensitively against the base name).
_SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "credentials", ".npmrc", ".pypirc",
)


def _is_secret_file(filename: str) -> bool:
    """Check if a file name matches a well-known secret file pattern."""
    name = filename.lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_FILE_PATTERNS)


# ---------------------------------------------------------------------------
# Mock Data
# ---------------------------------------------------------------------------

_MOCK_REPO_STRUCTURE = {
    "files": [
        {"path": "README.md", "type": "file", "size": 1024},
        {"path": ".gitignore", "type": "file", "size": 256},
        {"path": "services/api/package.json", "type": "file", "size": 512},
        {"path": "services/api/index.js", "type": "file", "size": 2048},
        {"path": "services/api/Dockerfile", "type": "file", "size": 384},
        {"path": "services/worker/requirements.txt", "type": "file", "size": 256},
        {"path": "services/worker/app.py", "type": "file", "size": 1536},
        {"path": "services/worker/Dockerfile", "type": "file", "size": 384},
        {"path": "infrastructure/main.tf", "type": "file", "size": 2048},
    ],
    "directories": [
        "services",
        "services/api",
        "services/worker",
        "infrastructure",
    ],
}

_MOCK_FILE_CONTENTS = {
    "services/api/package.json": json.dumps({
        "name": "api-service",
        "version": "1.0.0",
        "dependencies": {
            "express": "^4.18.0",
            "pg": "^8.11.0",
            "redis": "^4.6.0",
            "aws-sdk": "^2.1400.0",
        },
    }, indent=2),
    "services/worker/requirements.txt": "fastapi==0.104.0\ncelery==5.3.0\nredis==5.0.0\nboto3==1.28.0\npsycopg2-binary==2.9.9",
}


# ---------------------------------------------------------------------------
# TOOL 1 — scan_repository_structure
# ---------------------------------------------------------------------------

@tool
def scan_repository_structure(repo_path: str) -> str:
    """
    Scan a git repository and return its file structure.

    This tool lists all files and directories in the repository, excluding
    git-ignored files and common build artifacts. It identifies dependency
    files, configuration files, and source code to help understand the
    repository layout.

    Use this as the first step when analyzing a new repository.

    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root directory.

    Returns
    -------
    str
        JSON with file tree, dependency files, config files, and statistics.
    """
    if _MOCK:
        return _wrap(_MOCK_REPO_STRUCTURE, "scan_repository_structure")

    try:
        repo_path_obj = Path(repo_path).resolve()
        if not repo_path_obj.exists():
            return _wrap({"error": f"Repository path does not exist: {repo_path}"}, "scan_repository_structure")

        if not (repo_path_obj / ".git").exists():
            return _wrap({"error": f"Not a git repository: {repo_path}"}, "scan_repository_structure")

        files = []
        directories = set()
        dependency_files = []
        config_files = []

        # Walk the repository
        for root, dirs, filenames in os.walk(repo_path_obj):
            # Skip common ignored directories
            dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target"}]

            rel_root = Path(root).relative_to(repo_path_obj)
            if str(rel_root) != ".":
                directories.add(str(rel_root))

            for filename in filenames:
                file_path = Path(root) / filename
                rel_path = file_path.relative_to(repo_path_obj)
                rel_path_str = str(rel_path)

                file_info = {
                    "path": rel_path_str,
                    "type": "file",
                    "size": file_path.stat().st_size,
                }
                files.append(file_info)

                if _is_dependency_file(filename):
                    dependency_files.append(rel_path_str)
                if _is_config_file(filename):
                    config_files.append(rel_path_str)

        return _wrap({
            "repo_path": str(repo_path_obj),
            "total_files": len(files),
            "total_directories": len(directories),
            "files": files[:100],  # Limit to first 100 for context window
            "directories": sorted(list(directories))[:50],
            "dependency_files": dependency_files,
            "config_files": config_files,
        }, "scan_repository_structure")

    except Exception as exc:
        return _wrap({"error": str(exc)}, "scan_repository_structure")


# ---------------------------------------------------------------------------
# TOOL 2 — read_file_content
# ---------------------------------------------------------------------------

def _read_file_content_impl(repo_path: str, file_path: str) -> str:
    """Internal implementation of read_file_content."""
    if _MOCK:
        content = _MOCK_FILE_CONTENTS.get(file_path)
        if content:
            return _wrap({"file_path": file_path, "content": content, "size": len(content)}, "read_file_content")
        return _wrap({"error": f"File not found in mock data: {file_path}"}, "read_file_content")

    try:
        repo_path_obj = Path(repo_path).resolve()
        full_path = (repo_path_obj / file_path).resolve()

        # Security: ensure file is within repo. A path-component check, so a
        # sibling such as /repo-secrets does not pass for /repo.
        if not full_path.is_relative_to(repo_path_obj):
            return _wrap({"error": "File path outside repository"}, "read_file_content")

        # Security: never hand likely secret files to the model.
        if _is_secret_file(full_path.name):
            return _wrap({"error": "Refusing to read a likely secret file"}, "read_file_content")

        if not full_path.exists():
            return _wrap({"error": f"File not found: {file_path}"}, "read_file_content")

        # Read file (limit size for context window)
        max_size = 50000  # ~50KB
        file_size = full_path.stat().st_size
        if file_size > max_size:
            return _wrap({
                "error": f"File too large ({file_size} bytes, max {max_size})",
                "hint": "File is too large to read completely. Consider reading specific sections.",
            }, "read_file_content")

        content = full_path.read_text(encoding="utf-8", errors="ignore")

        return _wrap({
            "file_path": file_path,
            "content": content,
            "size": file_size,
        }, "read_file_content")

    except Exception as exc:
        return _wrap({"error": str(exc)}, "read_file_content")


@tool
def read_file_content(repo_path: str, file_path: str) -> str:
    """
    Read the content of a specific file in the repository.

    Use this to read dependency files (package.json, requirements.txt),
    configuration files (Dockerfile, terraform files), or source code
    to understand the application stack.

    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root directory.
    file_path : str
        Relative path to the file within the repository.

    Returns
    -------
    str
        JSON with file content and metadata.
    """
    return _read_file_content_impl(repo_path, file_path)


# ---------------------------------------------------------------------------
# TOOL 3 — detect_applications
# ---------------------------------------------------------------------------

@tool
def detect_applications(repo_path: str, file_tree: str) -> str:
    """
    Detect distinct applications or services in the repository.

    Analyzes the repository structure to identify separate applications,
    typically by finding dependency files (package.json, requirements.txt)
    or Dockerfiles in different directories.

    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root directory.
    file_tree : str
        JSON output from scan_repository_structure tool.

    Returns
    -------
    str
        JSON with detected applications and their locations.
    """
    if _MOCK:
        return _wrap({
            "applications": [
                {
                    "name": "api-service",
                    "path": "services/api",
                    "indicators": ["package.json", "Dockerfile"],
                    "type": "service",
                },
                {
                    "name": "worker-service",
                    "path": "services/worker",
                    "indicators": ["requirements.txt", "Dockerfile"],
                    "type": "service",
                },
            ],
            "total_applications": 2,
        }, "detect_applications")

    try:
        tree_data = json.loads(file_tree)
        if "error" in tree_data.get("data", {}):
            return _wrap({"error": "Invalid file tree data"}, "detect_applications")

        data = tree_data.get("data", {})
        dependency_files = data.get("dependency_files", [])
        config_files = data.get("config_files", [])

        # Group by directory
        app_dirs = {}
        for dep_file in dependency_files:
            dir_path = str(Path(dep_file).parent)
            if dir_path not in app_dirs:
                app_dirs[dir_path] = {"indicators": [], "path": dir_path}
            app_dirs[dir_path]["indicators"].append(Path(dep_file).name)

        # Add Dockerfiles as indicators
        for config_file in config_files:
            if "Dockerfile" in config_file:
                dir_path = str(Path(config_file).parent)
                if dir_path in app_dirs:
                    app_dirs[dir_path]["indicators"].append("Dockerfile")

        # Convert to application list
        applications = []
        for dir_path, info in app_dirs.items():
            app_name = Path(dir_path).name if dir_path != "." else "root-app"
            applications.append({
                "name": app_name,
                "path": dir_path,
                "indicators": info["indicators"],
                "type": "service" if "Dockerfile" in info["indicators"] else "library",
            })

        return _wrap({
            "applications": applications,
            "total_applications": len(applications),
        }, "detect_applications")

    except Exception as exc:
        return _wrap({"error": str(exc)}, "detect_applications")


# ---------------------------------------------------------------------------
# TOOL 4 — analyze_dependencies
# ---------------------------------------------------------------------------

@tool
def analyze_dependencies(repo_path: str, app_path: str, dependency_file: str) -> str:
    """
    Parse a dependency file and extract the list of dependencies.

    Supports package.json (Node.js), requirements.txt (Python), go.mod (Go),
    and other common dependency formats.

    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root directory.
    app_path : str
        Relative path to the application directory.
    dependency_file : str
        Name of the dependency file (e.g., "package.json").

    Returns
    -------
    str
        JSON with parsed dependencies, language, and framework detection.
    """
    file_path = f"{app_path}/{dependency_file}" if app_path != "." else dependency_file

    # Read the file using internal implementation
    file_content_result = _read_file_content_impl(repo_path, file_path)
    file_data = json.loads(file_content_result).get("data", {})

    if "error" in file_data:
        return _wrap(file_data, "analyze_dependencies")

    content = file_data.get("content", "")
    dependencies = []
    language = "unknown"
    framework = None

    try:
        if dependency_file == "package.json":
            language = "Node.js"
            pkg = json.loads(content)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            dependencies = list(deps.keys())
            
            # Detect framework
            if "express" in deps:
                framework = "Express"
            elif "next" in deps:
                framework = "Next.js"
            elif "react" in deps:
                framework = "React"

        elif dependency_file == "requirements.txt":
            language = "Python"
            for line in content.split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    dep = re.split(r"[=<>!]", line)[0].strip()
                    dependencies.append(dep)
            
            # Detect framework
            if "fastapi" in dependencies:
                framework = "FastAPI"
            elif "flask" in dependencies:
                framework = "Flask"
            elif "django" in dependencies:
                framework = "Django"

        elif dependency_file == "go.mod":
            language = "Go"
            for line in content.split("\n"):
                if line.strip().startswith("require"):
                    continue
                match = re.match(r'\s+([^\s]+)\s+v', line)
                if match:
                    dependencies.append(match.group(1))

        return _wrap({
            "app_path": app_path,
            "dependency_file": dependency_file,
            "language": language,
            "framework": framework,
            "dependencies": dependencies,
            "total_dependencies": len(dependencies),
        }, "analyze_dependencies")

    except Exception as exc:
        return _wrap({"error": str(exc)}, "analyze_dependencies")


# ---------------------------------------------------------------------------
# TOOL 5 — map_aws_services
# ---------------------------------------------------------------------------

@tool
def map_aws_services(dependencies: str) -> str:
    """
    Map application dependencies to required AWS services.

    Analyzes the dependency list and identifies which AWS services would be
    needed to run the application (RDS, ElastiCache, S3, etc.).

    Parameters
    ----------
    dependencies : str
        JSON output from analyze_dependencies tool.

    Returns
    -------
    str
        JSON with AWS service requirements mapped from dependencies.
    """
    try:
        deps_data = json.loads(dependencies).get("data", {})
        if "error" in deps_data:
            return _wrap(deps_data, "map_aws_services")

        dep_list = deps_data.get("dependencies", [])
        language = deps_data.get("language", "unknown")
        framework = deps_data.get("framework")

        aws_services = {}
        matched_dependencies = []

        # Map dependencies to AWS services
        for dep in dep_list:
            dep_lower = dep.lower()
            for key, service_info in DEPENDENCY_TO_AWS_SERVICE.items():
                if key in dep_lower:
                    category = service_info["category"]
                    if category not in aws_services:
                        aws_services[category] = []
                    
                    service_entry = {
                        "service": service_info["service"],
                        "engine": service_info["engine"],
                        "dependency": dep,
                    }
                    
                    if service_entry not in aws_services[category]:
                        aws_services[category].append(service_entry)
                        matched_dependencies.append(dep)

        # Add compute requirements based on framework
        if framework or language != "unknown":
            aws_services["compute"] = [{
                "service": "ECS Fargate or Lambda",
                "engine": language,
                "framework": framework,
                "note": "Container or serverless compute for application runtime",
            }]

        # Add networking (always needed for services)
        if aws_services:
            aws_services["networking"] = [{
                "service": "VPC",
                "components": ["Subnets", "Security Groups", "NAT Gateway"],
                "note": "Network isolation and security",
            }]
            
            if framework:  # Web services need load balancer
                aws_services["networking"].append({
                    "service": "Application Load Balancer",
                    "note": "HTTP/HTTPS traffic distribution",
                })

        return _wrap({
            "language": language,
            "framework": framework,
            "total_dependencies": len(dep_list),
            "matched_dependencies": matched_dependencies,
            "aws_services": aws_services,
            "summary": {
                "databases": [s["service"] for s in aws_services.get("database", [])],
                "caching": [s["service"] for s in aws_services.get("cache", [])],
                "storage": [s["service"] for s in aws_services.get("storage", [])],
                "queuing": [s["service"] for s in aws_services.get("queue", [])],
                "compute": [s["service"] for s in aws_services.get("compute", [])],
            },
        }, "map_aws_services")

    except Exception as exc:
        return _wrap({"error": str(exc)}, "map_aws_services")


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    scan_repository_structure,
    read_file_content,
    detect_applications,
    analyze_dependencies,
    map_aws_services,
]
