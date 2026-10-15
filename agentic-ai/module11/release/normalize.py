# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Normalization by file type.

Text files: UTF-8 (BOM stripped), LF line endings, no trailing whitespace, exactly one final
newline. JSON and YAML: their canonical JSON form, so key order, formatting and YAML comments
never change a hash. Anything else: raw bytes. Errors name the path, never the contents.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

import yaml

TEXT_SUFFIXES = {".md", ".txt", ".py", ".mjs", ".toml", ".sh", ".lock", ".cfg"}
TEXT_NAMES = {"Dockerfile", "CODEOWNERS"}


class NormalizationError(ValueError):
    """A file could not be normalized; fatal for a build."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def canonical_json(value: Any) -> bytes:
    """Sorted keys, compact separators, UTF-8 (the one canonical form used everywhere)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def kind_of(path: str) -> str:
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    if suffix in {".json", ".jsonl"}:
        return suffix[1:]
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix in TEXT_SUFFIXES or p.name in TEXT_NAMES or p.name.startswith("Dockerfile"):
        return "text"
    return "raw"


def _decode(path: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NormalizationError(path, f"invalid UTF-8 at byte {exc.start}") from None
    return text[1:] if text.startswith("\ufeff") else text


def normalize_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "\n".join(line.rstrip() for line in lines).strip("\n")
    return body + "\n"


def normalize(path: str, data: bytes) -> bytes:
    """Return the normalized bytes of one file; raise NormalizationError on invalid input."""
    kind = kind_of(path)
    if kind == "raw":
        return data
    text = _decode(path, data)
    if kind == "text":
        return normalize_text(text).encode("utf-8")
    if kind == "json":
        try:
            return canonical_json(json.loads(text))
        except json.JSONDecodeError as exc:
            raise NormalizationError(path, f"invalid JSON at line {exc.lineno}") from None
    if kind == "jsonl":
        out = []
        for number, line in enumerate(normalize_text(text).splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(canonical_json(json.loads(line)))
            except json.JSONDecodeError:
                raise NormalizationError(path, f"invalid JSON on line {number}") from None
        return b"\n".join(out) + b"\n"
    try:
        return canonical_json(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise NormalizationError(path, f"invalid YAML{where}") from None


def load_document(path: str, data: bytes) -> Any:
    """Parse a JSON or YAML file through normalization (so errors are NormalizationError)."""
    return json.loads(normalize(path, data))
