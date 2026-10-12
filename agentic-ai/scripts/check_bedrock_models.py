#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
scripts/check_bedrock_models.py
================================
Maintainer tool, run before each module release.

Scans the code sample for Amazon Bedrock model IDs and checks each one's
lifecycle with read-only Bedrock control-plane calls:

- every base model ID (the part after any ``us.``/``eu.``/``apac.``/``global.``
  prefix) is looked up with ``bedrock:GetFoundationModel``;
- every prefixed ID must also exist as a system-defined inference profile
  (``bedrock:ListInferenceProfiles``).

It prints one row per ID and exits 1 if any ID is LEGACY, past its end-of-life
date, or unknown to Bedrock in the chosen Region. Nothing is created or
modified.

USAGE
-----
  # Scan agentic-ai/ (the default) in the current Region
  python scripts/check_bedrock_models.py

  # Scan specific files or directories, in another Region
  python scripts/check_bedrock_models.py module5 demos/module5_demo.py --region us-west-2

Skipped while scanning: docs/history/, CHANGELOG.md, node_modules/, .cache/,
.local/, virtualenvs, .env files, this script and its test, test lines that
assert an ID is absent (``... not in ...``), and any line containing the
marker ``check-bedrock-models: ignore``.

Required IAM permissions: bedrock:GetFoundationModel and
bedrock:ListInferenceProfiles.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent  # agentic-ai/

MODEL_ID_RE = re.compile(
    r"(?<![\w.@/-])"
    r"(?:(?:us|eu|apac|global)\.)?"
    r"(?:anthropic|amazon|meta|openai|cohere|mistral|deepseek)\."
    r"[a-z0-9][a-z0-9.-]*[a-z0-9](?::\d+)?"
)
PREFIX_RE = re.compile(r"^(?:us|eu|apac|global)\.")
IGNORE_MARKER = "check-bedrock-models: ignore"

SKIP_DIR_NAMES = {
    ".git", "node_modules", ".cache", ".local", "__pycache__", "artifacts",
    ".venv", "venv", ".pytest_cache",
}
SKIP_REL_PATHS = {"docs/history"}
SKIP_FILE_NAMES = {"CHANGELOG.md", "check_bedrock_models.py", "test_check_bedrock_models.py"}
TEXT_SUFFIXES = {
    ".py", ".md", ".mjs", ".js", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".txt", ".sh", ".html", ".example", ".cfg", ".ini",
}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _is_model_id(candidate: str) -> bool:
    """Bedrock model IDs always contain a hyphen after the provider (filters ``meta.get``)."""
    model_part = PREFIX_RE.sub("", candidate).split(".", 1)[1]
    return "-" in model_part


def _skip_dir(path: Path, base: Path) -> bool:
    if path.name in SKIP_DIR_NAMES or path.name.startswith(".venv"):
        return True
    rel = path.relative_to(base).as_posix()
    return any(rel == p or rel.endswith("/" + p) for p in SKIP_REL_PATHS)


def _skip_file(path: Path) -> bool:
    name = path.name
    if name in SKIP_FILE_NAMES:
        return True
    if name.startswith(".env") and not name.endswith(".example"):
        return True  # never read real environment files
    return path.suffix not in TEXT_SUFFIXES and name not in {"Dockerfile", "Makefile"}


def _iter_files(paths: Iterable[Path]) -> Iterable[tuple[Path, Path]]:
    """Yield (file, base) pairs for every scannable file under *paths*."""
    for path in paths:
        if path.is_file():
            if not _skip_file(path):
                yield path, path.parent
            continue
        for root, dirs, files in os.walk(path):
            root_path = Path(root)
            dirs[:] = sorted(d for d in dirs if not _skip_dir(root_path / d, path))
            for name in sorted(files):
                file_path = root_path / name
                if not _skip_file(file_path):
                    yield file_path, path


def scan(paths: Iterable[Path], display_root: Path | None = None) -> dict[str, set[str]]:
    """Return {model_id: {relative file paths}} for every ID found under *paths*."""
    found: dict[str, set[str]] = {}
    for file_path, base in _iter_files(paths):
        try:
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        in_tests = "tests" in file_path.parts
        for line in text.splitlines():
            if IGNORE_MARKER in line:
                continue
            if in_tests and " not in " in line:
                continue  # tests asserting an ID is absent
            for match in MODEL_ID_RE.finditer(line):
                model_id = match.group(0)
                if not _is_model_id(model_id):
                    continue
                shown_root = display_root or base
                try:
                    shown = file_path.resolve().relative_to(shown_root.resolve()).as_posix()
                except ValueError:
                    shown = file_path.as_posix()
                found.setdefault(model_id, set()).add(shown)
    return found


# ---------------------------------------------------------------------------
# Lifecycle checks
# ---------------------------------------------------------------------------

@dataclass
class Row:
    model_id: str
    kind: str
    status: str
    legacy_time: str = ""
    end_of_life_time: str = ""
    files: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status != "ACTIVE"


def _fmt_time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value) if value else ""


def _lifecycle(client: Any, base_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return {'status', 'legacy', 'eol'} for a base model ID (status UNKNOWN if not found)."""
    if base_id in cache:
        return cache[base_id]
    from botocore.exceptions import ClientError

    try:
        details = client.get_foundation_model(modelIdentifier=base_id)["modelDetails"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"ResourceNotFoundException", "ValidationException"}:
            cache[base_id] = {"status": "UNKNOWN", "legacy": None, "eol": None}
            return cache[base_id]
        raise
    lifecycle = details.get("modelLifecycle") or {}
    status = lifecycle.get("status", "UNKNOWN")
    eol = lifecycle.get("endOfLifeTime")
    if isinstance(eol, datetime) and eol <= datetime.now(timezone.utc):
        status = "EOL"
    cache[base_id] = {"status": status, "legacy": lifecycle.get("legacyTime"), "eol": eol}
    return cache[base_id]


def _inference_profiles(client: Any) -> dict[str, str]:
    """Return {inferenceProfileId: status} for all system-defined profiles."""
    profiles: dict[str, str] = {}
    paginator = client.get_paginator("list_inference_profiles")
    for page in paginator.paginate(typeEquals="SYSTEM_DEFINED"):
        for summary in page.get("inferenceProfileSummaries", []):
            profiles[summary["inferenceProfileId"]] = summary.get("status", "UNKNOWN")
    return profiles


def check(found: dict[str, set[str]], client: Any) -> list[Row]:
    """Look up every found ID and return one Row per ID, sorted by ID."""
    cache: dict[str, dict[str, Any]] = {}
    profiles: dict[str, str] | None = None
    rows: list[Row] = []
    for model_id in sorted(found):
        files = sorted(found[model_id])
        base_id = PREFIX_RE.sub("", model_id)
        info = _lifecycle(client, base_id, cache)
        status = info["status"]
        if base_id != model_id:
            kind = "inference-profile"
            if profiles is None:
                profiles = _inference_profiles(client)
            profile_status = profiles.get(model_id)
            if profile_status is None:
                status = "UNKNOWN (no inference profile)"
            elif profile_status != "ACTIVE":
                status = f"{profile_status} (inference profile)"
        else:
            kind = "foundation-model"
        rows.append(Row(
            model_id=model_id,
            kind=kind,
            status=status,
            legacy_time=_fmt_time(info["legacy"]),
            end_of_life_time=_fmt_time(info["eol"]),
            files=files,
        ))
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _files_cell(files: list[str], limit: int = 3) -> str:
    shown = ", ".join(files[:limit])
    return shown + (f" (+{len(files) - limit} more)" if len(files) > limit else "")


def print_table(rows: list[Row], out: Any = None) -> None:
    out = out or sys.stdout
    headers = ["id", "kind", "lifecycle", "legacyTime", "endOfLifeTime", "files"]
    table = [
        [r.model_id, r.kind, r.status, r.legacy_time or "-", r.end_of_life_time or "-", _files_cell(r.files)]
        for r in rows
    ]
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths[:-1]) + "  {}"
    print(fmt.format(*headers), file=out)
    print(fmt.format(*("-" * w for w in widths)), file=out)
    for row in table:
        print(fmt.format(*row), file=out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"


def main(argv: list[str] | None = None, client: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the lifecycle status of every Bedrock model ID used in the code sample (read-only).",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help=f"Files or directories to scan (default: {ROOT})",
    )
    parser.add_argument(
        "--region", default=_default_region(),
        help="Bedrock Region (default: AWS_REGION, then AWS_DEFAULT_REGION, then us-east-1)",
    )
    args = parser.parse_args(argv)

    paths = args.paths or [ROOT]
    missing = [p for p in paths if not p.exists()]
    if missing:
        parser.error(f"path not found: {', '.join(str(p) for p in missing)}")

    found = scan(paths, display_root=ROOT if not args.paths else None)
    if not found:
        print("No Bedrock model IDs found.")
        return 0

    if client is None:
        import boto3

        client = boto3.client("bedrock", region_name=args.region)

    from botocore.exceptions import BotoCoreError, ClientError

    try:
        rows = check(found, client)
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: Bedrock lookup failed in {args.region}: {exc}", file=sys.stderr)
        return 2

    print(f"Bedrock model lifecycle check ({args.region}), {len(rows)} IDs\n")
    print_table(rows)
    failed = [r for r in rows if r.failed]
    print()
    if failed:
        print(f"FAIL: {len(failed)} ID(s) are LEGACY, EOL or unknown: "
              + ", ".join(r.model_id for r in failed))
        return 1
    print("OK: every model ID is ACTIVE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
