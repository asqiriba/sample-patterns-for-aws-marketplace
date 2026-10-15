# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validated loaders for the harness configuration files and the scenario suite.

Each loader returns plain data after checking shape and values, and raises ConfigError with a
message that names the file and the field. Fatal at build.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import AGENT, HARNESS, SINGLE
from .naming import SCENARIO_ID
from .normalize import load_document

CATEGORIES = ("happy_path", "edge_cases", "regressions", "adversarial")
SCENARIO_DIR = f"{HARNESS}/evaluation/scenarios"
SCENARIO_KEYS = {"scenario_id", "category", "turns", "expected_trajectory", "assertions", "expected_response",
                 "service", "team", "tags"}
EVAL_SCOPES = ("smoke", "tagged", "retrieval", "full", "rebaseline", "evalset")
ROLLOUTS = ("none", "full", "canary-if-runtime-changed", "canary", "shadow-then-canary")
RUNTIME_VERSIONS = ("none", "maybe", "config", "image")
APPROVALS = ("default", "required")
HANDLERS = ("none", "tool", "tool_if_limits", "all")


class ConfigError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def read(root: str | Path, rel: str) -> Any:
    path = Path(root) / rel
    if not path.is_file():
        raise ConfigError(rel, "missing")
    return load_document(rel, path.read_bytes())


def _require(cond: bool, path: str, message: str) -> None:
    if not cond:
        raise ConfigError(path, message)


def orchestration(root) -> dict:
    rel = SINGLE["orchestration"]
    doc = read(root, rel)
    _require(isinstance(doc, dict), rel, "must be a mapping")
    for key in ("maxTurns", "maxToolCalls", "deadlineSeconds"):
        _require(isinstance(doc.get(key), int) and doc[key] > 0, rel, f"{key} must be a positive integer")
    _require(doc["deadlineSeconds"] <= 900, rel, "deadlineSeconds must be <= 900 (runtime lifecycle)")
    req = doc.get("requiredTools")
    _require(isinstance(req, dict) and all(isinstance(v, list) for v in req.values()), rel,
             "requiredTools must map agent -> [tool]")
    _require(isinstance(doc.get("allowUnreferenced", []), list), rel, "allowUnreferenced must be a list")
    _require(isinstance(doc.get("promptVars", {}), dict), rel, "promptVars must be a mapping")
    return doc


def models(root) -> dict:
    rel = SINGLE["model"]
    doc = read(root, rel)
    _require(isinstance(doc, dict) and doc, rel, "must map agent -> model settings")
    for agent, spec in doc.items():
        _require(isinstance(spec, dict), rel, f"{agent} must be a mapping")
        _require(isinstance(spec.get("modelId"), str) and spec["modelId"], rel, f"{agent}.modelId is required")
        _require(isinstance(spec.get("maxOutputTokens"), int) and 1 <= spec["maxOutputTokens"] <= 32000, rel,
                 f"{agent}.maxOutputTokens must be 1-32000")
        _require(isinstance(spec.get("promptBudgetTokens"), int), rel, f"{agent}.promptBudgetTokens is required")
    return doc


def guardrail(root) -> dict:
    rel = SINGLE["guardrail"]
    doc = read(root, rel)
    _require(isinstance(doc, dict), rel, "must be a mapping")
    _require(doc.get("pii_handling") in {"BLOCK", "ANONYMIZE"}, rel, "pii_handling must be BLOCK or ANONYMIZE")
    _require(isinstance(doc.get("pii_entities", []), list), rel, "pii_entities must be a list")
    for topic in doc.get("denied_topics", []):
        _require(isinstance(topic, dict) and {"name", "definition"} <= set(topic), rel,
                 "denied_topics entries need name and definition")
    _require(isinstance(doc.get("content_filters", {}), dict), rel, "content_filters must map category -> strength")
    for category, strength in doc.get("content_filters", {}).items():
        _require(strength in {"NONE", "LOW", "MEDIUM", "HIGH"}, rel, f"content_filters.{category} has an invalid strength")
    return doc


def external_surfaces(root) -> list[dict]:
    rel = SINGLE["external_surfaces"]
    doc = read(root, rel)
    surfaces = (doc or {}).get("surfaces", [])
    _require(isinstance(surfaces, list), rel, "surfaces must be a list")
    for s in surfaces:
        _require(isinstance(s, dict) and {"id", "kind"} <= set(s), rel, "each surface needs id and kind")
    return surfaces


def retrieval(root) -> list[dict]:
    rel = SINGLE["retrieval"]
    corpora = (read(root, rel) or {}).get("corpora", [])
    _require(isinstance(corpora, list) and all(isinstance(c, dict) and "corpus" in c for c in corpora), rel,
             "corpora must be a list of {corpus, snapshotId, ...}")
    return corpora


@dataclass(frozen=True)
class Rule:
    name: str
    match: tuple[str, ...]
    image: bool
    handlers: str
    bundle: bool
    runtimeVersion: str
    evalScope: str
    approval: str
    rollout: str
    repeats: dict
    releaseKind: str
    note: str | None


def _rule(spec: dict, rel: str) -> Rule:
    name = spec.get("name", "?")
    for key, allowed in (("handlers", HANDLERS), ("runtimeVersion", RUNTIME_VERSIONS), ("evalScope", EVAL_SCOPES),
                         ("approval", APPROVALS), ("rollout", ROLLOUTS)):
        _require(spec.get(key) in allowed, rel, f"rule {name}: {key} must be one of {', '.join(allowed)}")
    return Rule(name, tuple(spec.get("match", ())), bool(spec.get("image")), spec["handlers"], bool(spec.get("bundle")),
                spec["runtimeVersion"], spec["evalScope"], spec["approval"], spec["rollout"],
                dict(spec.get("repeats") or {}), spec.get("releaseKind", "deploy"), spec.get("note"))


def change_types(root) -> tuple[list[Rule], Rule, int]:
    rel = f"{HARNESS}/pipeline/change_types.yaml"
    doc = read(root, rel)
    _require(isinstance(doc, dict) and isinstance(doc.get("rules"), list), rel, "rules must be a list")
    _require(isinstance(doc.get("revision"), int), rel, "revision must be an integer")
    rules = [_rule(r, rel) for r in doc["rules"]]
    _require(len({r.name for r in rules}) == len(rules), rel, "rule names must be unique")
    return rules, _rule(doc.get("unknown") or {}, rel), doc["revision"]


def thresholds(root) -> dict:
    rel = f"{HARNESS}/evaluation/thresholds.yaml"
    doc = read(root, rel)
    _require(isinstance(doc, dict), rel, "must be a mapping")
    _require(isinstance(doc.get("evaluatorSetVersion"), str), rel, "evaluatorSetVersion must be a string")
    _require(doc.get("baselineManifest") is None or isinstance(doc["baselineManifest"], str), rel,
             "baselineManifest must be a manifestVersion or null")
    for profile in ("preview", "staging"):
        _require(profile in (doc.get("profiles") or {}), rel, f"profiles.{profile} is required")
    for gate in (doc.get("gates") or {}).get("hard", []) + (doc.get("gates") or {}).get("statistical", []):
        _require("evaluator" in gate and "scope" in gate, rel, "every gate needs evaluator and scope")
    return doc


def evaluators(root) -> dict:
    rel = f"{HARNESS}/evaluation/evaluators.yaml"
    doc = read(root, rel)
    _require(isinstance(doc, dict) and isinstance(doc.get("version"), str), rel, "version must be a string")
    return doc


def scenarios(root) -> list[dict]:
    """Load and validate the four JSONL files (scenario line)."""
    base = Path(root) / SCENARIO_DIR
    rows: list[dict] = []
    seen: set[str] = set()
    for category in CATEGORIES:
        rel = f"{SCENARIO_DIR}/{category}.jsonl"
        path = base / f"{category}.jsonl"
        if not path.is_file():
            raise ConfigError(rel, "missing")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            where = f"{rel}:{number}"
            row = load_document(where + ".json", line.encode("utf-8"))
            _require(isinstance(row, dict), where, "must be a JSON object")
            _require(not (set(row) - SCENARIO_KEYS), where, f"unknown keys {sorted(set(row) - SCENARIO_KEYS)}")
            sid = row.get("scenario_id")
            _require(isinstance(sid, str) and bool(SCENARIO_ID.match(sid)), where, "scenario_id must match ^[a-z0-9-]{3,64}$")
            _require(sid not in seen, where, f"duplicate scenario_id {sid}")
            seen.add(sid)
            _require(row.get("category") == category, where, f"category must be {category}")
            turns = row.get("turns")
            _require(isinstance(turns, list) and 1 <= len(turns) <= 3, where, "turns must have 1-3 entries")
            for turn in turns:
                _require(isinstance(turn, dict) and isinstance(turn.get("input"), str) and 0 < len(turn["input"]) <= 4000,
                         where, "each turn needs an input of 1-4000 characters")
            _require(isinstance(row.get("tags", []), list), where, "tags must be a list")
            rows.append(row)
    return rows
