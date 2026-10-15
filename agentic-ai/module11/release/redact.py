# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Redaction for harvested sessions and committed captures. Pure.

Module 7's `anonymize_pii` is reused without its import chain: `module7/__init__.py` imports the
agent framework, so `module7/memory/guardrails.py` (which imports only `os` and `re`) is loaded
by file path. Account IDs, ARNs and the given user names are then replaced with fixture values,
and `diff()` renders what changed for the MR description.
"""
from __future__ import annotations

import difflib
import importlib.util
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

GUARDRAILS_PATH = "module7/memory/guardrails.py"
FIXTURE_ACCOUNT = "111122223333"
FIXTURE_NAMES = ("presenter", "reviewer", "operator", "teammate")
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
_ARN = re.compile(r"\barn:(aws[a-z-]*):([a-z0-9-]+):([a-z0-9-]*):(\d{12})?:")


@lru_cache(maxsize=4)
def load_anonymize(repo_root: str) -> Callable[[str], str]:
    """`anonymize_pii` from Module 7, loaded by path (no module7 package import, no langchain)."""
    path = Path(repo_root) / GUARDRAILS_PATH
    spec = importlib.util.spec_from_file_location("m7_guardrails", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.anonymize_pii


def redact_text(text: str, anonymize: Callable[[str], str], names: Sequence[str] = ()) -> str:
    text = anonymize(text)
    text = _ARN.sub(lambda m: f"arn:{m.group(1)}:{m.group(2)}:{m.group(3)}:{FIXTURE_ACCOUNT if m.group(4) else ''}:",
                    text)
    text = _ACCOUNT.sub(FIXTURE_ACCOUNT, text)
    for index, name in enumerate(sorted({n for n in names if n}, key=len, reverse=True)):
        text = re.sub(rf"(?i)\b{re.escape(name)}\b", FIXTURE_NAMES[index % len(FIXTURE_NAMES)], text)
    return text


def redact(value: Any, anonymize: Callable[[str], str], names: Sequence[str] = ()) -> Any:
    """Redact every string in a JSON-shaped value (keys are kept)."""
    if isinstance(value, Mapping):
        return {k: redact(v, anonymize, names) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, anonymize, names) for v in value]
    if isinstance(value, str):
        return redact_text(value, anonymize, names)
    return value


INCIDENT_KEYS = ("service", "team", "turns", "expected_trajectory", "assertions", "expected_response", "tags",
                 "category", "scenario_id")


def incident_shape(session: Mapping[str, Any]) -> dict:
    """Reduce a harvested session to the incident shape a scenario needs; drop everything else."""
    return {k: session[k] for k in INCIDENT_KEYS if k in session}


def diff(before: Any, after: Any) -> str:
    a = json.dumps(before, indent=1, sort_keys=True).splitlines()
    b = json.dumps(after, indent=1, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "harvested", "redacted", lineterm=""))
