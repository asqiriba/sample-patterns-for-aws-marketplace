# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Captures for the demo driver's `--record`, `--replay` and `--replay-fixtures`.

A capture is one beat's real run: `{"capture": "b5-canary", "section": 5, "recordedAt", "runIds": [...],
"cut": "full", "events": [{t, sub, kind, text, style}], "responses": [{t, sub, api, response}]}`.
`--record` writes module11/.local/captures/<capture>.json (git-ignored: it holds account identifiers).
`module11 captures export` copies them, redacted, into tests/fixtures/module11/captures/ (committed), which
`--replay-fixtures` renders with no credentials. Captures are only ever written from a real run; a beat with
no committed capture says so instead of rendering anything.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "module11/.local/captures"
FIXTURES = ROOT / "tests/fixtures/module11/captures"
SCHEMA = "m11-capture/1"
_URL = re.compile(r"https?://([A-Za-z0-9.-]+)")
PUBLIC_HOSTS = ("gitlab.com", "vercel.com", "docs.aws.amazon.com")


def capture_name(section: int, canary: bool = False) -> str:
    """b0-b7; B5's `r` fallback during the race is the canary capture, b5-canary (c.5)."""
    return "b5-canary" if canary else f"b{section}"


def path_for(name: str, directory: Path | None = None) -> Path:
    candidate = Path(name)
    if candidate.suffix == ".json" and candidate.exists():
        return candidate
    return (directory or LOCAL) / f"{name}.json"


def load(name: str, directory: Path | None = None) -> dict | None:
    path = path_for(name, directory)
    if not path.is_file():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{path.name} is not a {SCHEMA} capture")
    return doc


def save(doc: dict, directory: Path | None = None) -> Path:
    target = (directory or LOCAL)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{doc['capture']}.json"
    path.write_text(json.dumps({**doc, "schema": SCHEMA}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def mask_urls(text: str) -> str:
    return _URL.sub(lambda m: m.group(0) if m.group(1).endswith(PUBLIC_HOSTS) else
                    m.group(0).replace(m.group(1), "example.invalid"), text)


def redact_capture(doc: dict, *, anonymize, names: Iterable[str]) -> dict:
    """Account IDs, ARNs, user names (release/redact.py) and every non-public URL host."""
    from module11.release import redact as RD
    names = tuple(n for n in names if n)

    def one(value):
        if isinstance(value, dict):
            return {k: one(v) for k, v in value.items()}
        if isinstance(value, list):
            return [one(v) for v in value]
        if isinstance(value, str):
            return mask_urls(RD.redact_text(value, anonymize, names))
        return value
    return {**one(doc), "redacted": True}


def redaction_names() -> list[str]:
    """User names to replace: M11_REDACT_NAMES (comma-separated) plus the operator user from the environment."""
    names = [n.strip() for n in os.environ.get("M11_REDACT_NAMES", "").split(",") if n.strip()]
    for p in os.environ.get("M11_OPERATOR_PRINCIPALS", "").split(","):
        if "/" in p:
            names.append(p.rsplit("/", 1)[-1])
    return names


def export(root: Path = ROOT, *, source: Path | None = None, target: Path | None = None,
           names: Iterable[str] | None = None) -> list[Path]:
    """`module11 captures export`: every local capture, redacted, into the committed fixtures directory."""
    from module11.release import redact as RD
    anonymize = RD.load_anonymize(str(root))
    src = source or root / "module11/.local/captures"
    dst = target or root / "tests/fixtures/module11/captures"
    out = []
    for path in sorted(src.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA:
            continue
        clean = redact_capture(doc, anonymize=anonymize, names=redaction_names() if names is None else names)
        out.append(save(clean, dst))
    return out


def leaks(doc: dict, account: str) -> list[str]:
    """Strings in an exported capture that still hold the account ID (the export test's check)."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, str) and account in value:
            found.append(value)
    walk(doc)
    return found
