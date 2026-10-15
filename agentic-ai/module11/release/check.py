# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Build-stage checks C1-C16. Each returns Finding(code, path, message, severity);
any `error` fails the build. Pure: reads files under a repository root, never imports Module 5
(C11 parses its source with `ast`) and never calls AWS."""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import jsonschema
import yaml

from . import artifacts as A
from . import closure as closures
from . import codeowners
from . import detect as D
from . import stage as cfg
from .hashing import file_hash, files_hash, hex_of
from .normalize import NormalizationError, normalize
from .pins import REF_ACTIONS

KNOWN_TEAMS = ("platform", "payments")  # module10/flags.py LocalLaunchDarkly.status()
TOOL_REF = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")
PROMPT_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
GUARDRAIL_POLICY_SRC = "module5/engine/domain_adapter.py"
GUARDRAIL_EXTRA_KEYS = {"blocked_input_messaging", "blocked_outputs_messaging"}
CODEOWNERS_PATHS = (".gitlab/CODEOWNERS", "module11/gitlab/CODEOWNERS")
CI_PATHS = (".gitlab-ci.yml", "module11/gitlab/gitlab-ci.yml")
CI_IMAGE_INPUTS = ("module11/gitlab/ci-image", "module11/requirements-ci.txt")
C13_DIRS = tuple(f"module11/{d}" for d in ("release", "evaluation", "lambdas", "infrastructure", "pipeline",
                                           "gitlab", "vercel", "aws", "corpus"))
QUALITY_DIRS = tuple(f"module11/{d}" for d in ("harness/evaluation", "evaluation", "release", "lambdas"))
PLATFORM_PATHS = ("tests/conftest.py", "demos/module11_demo.py")
TOOL_KEYS = {"name", "agents", "kind", "mutating", "timeoutSeconds", "memoryMb", "requiresFlag", "shadowResult"}


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.code} {self.severity} {self.path}: {self.message}"


def _err(code, path, message):
    return Finding(code, path, message, "error")


def _read_json(root: Path, rel: str, code: str, out: list[Finding]):
    try:
        return cfg.read(root, rel)
    except (cfg.ConfigError, NormalizationError) as exc:
        out.append(_err(code, rel, str(exc).split(": ", 1)[-1]))
        return None


def _tools(root: Path) -> dict[str, dict]:
    metas = {}
    for tool in A.tool_names(root):
        try:
            metas[tool] = cfg.read(root, f"{A.TOOLS}/{tool}/tool.json")
        except (cfg.ConfigError, NormalizationError):
            metas[tool] = {}
    return metas


def offered(metas: Mapping[str, dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for tool, meta in metas.items():
        for agent in meta.get("agents", []) or []:
            out.setdefault(agent, set()).add(tool)
    return out


# ---------------------------------------------------------------- C1-C3, C6, C10
def c1(root: Path) -> list[Finding]:
    out = []
    for tool in A.tool_names(root):
        base = f"{A.TOOLS}/{tool}"
        for name in A.TOOL_FILES.values():
            if not (root / base / name).is_file():
                out.append(_err("C1", f"{base}/{name}", "missing tool definition file"))
        meta = _read_json(root, f"{base}/tool.json", "C1", out) if (root / base / "tool.json").is_file() else None
        if not isinstance(meta, dict):
            continue
        if meta.get("name") != tool:
            out.append(_err("C1", f"{base}/tool.json", f"name {meta.get('name')!r} does not equal the directory {tool!r}"))
        unknown = set(meta) - TOOL_KEYS
        if unknown:
            out.append(_err("C1", f"{base}/tool.json", f"unknown fields {sorted(unknown)}"))
        if meta.get("kind") not in {"lambda", "delegate"}:
            out.append(_err("C1", f"{base}/tool.json", "kind must be lambda or delegate"))
        if not meta.get("agents") or not set(meta["agents"]) <= {"companion", "planner"}:
            out.append(_err("C1", f"{base}/tool.json", "agents must be [companion] or [planner]"))
        if not isinstance(meta.get("mutating"), bool):
            out.append(_err("C1", f"{base}/tool.json", "mutating must be a boolean"))
        if not (isinstance(meta.get("timeoutSeconds"), int) and 1 <= meta["timeoutSeconds"] <= 30):
            out.append(_err("C1", f"{base}/tool.json", "timeoutSeconds must be 1-30"))
        if not (isinstance(meta.get("memoryMb"), int) and 128 <= meta["memoryMb"] <= 1024):
            out.append(_err("C1", f"{base}/tool.json", "memoryMb must be 128-1024"))
        handler = root / base / "handler"
        if meta.get("kind") == "lambda":
            for name in ("index.py", "logic.py"):
                if not (handler / name).is_file():
                    out.append(_err("C1", f"{base}/handler/{name}", "lambda tool is missing its handler file"))
        elif handler.exists():
            out.append(_err("C1", f"{base}/handler", "a delegate tool has no handler"))
        desc = root / base / "description.md"
        if desc.is_file():
            text = normalize("d.md", desc.read_bytes()).decode("utf-8").strip()
            if not 20 <= len(text) <= 600:
                out.append(_err("C1", f"{base}/description.md", f"description is {len(text)} characters (20-600)"))
    return out


def c2(root: Path) -> list[Finding]:
    out = []
    for tool in A.tool_names(root):
        rel = f"{A.TOOLS}/{tool}/schema.json"
        if not (root / rel).is_file():
            continue
        schema = _read_json(root, rel, "C2", out)
        if schema is None:
            continue
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            out.append(_err("C2", rel, f"invalid JSON Schema: {exc.message}"))
            continue
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False \
                or not schema.get("required"):
            out.append(_err("C2", rel, "top level must be type object, additionalProperties false, required non-empty"))
    return out


def run_signature(source: str) -> tuple[list[str], list[str]] | None:
    """(all params, required params) of `run()`, excluding a trailing `data` parameter."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            args = node.args
            positional = [a.arg for a in args.posonlyargs + args.args]
            defaults_from = len(positional) - len(args.defaults)
            required = [a for i, a in enumerate(positional) if i < defaults_from]
            kwonly = [a.arg for a in args.kwonlyargs]
            required += [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
            params = positional + kwonly
            if params and params[-1] == "data":
                params = params[:-1]
                required = [r for r in required if r != "data"]
            return params, required
    return None


def c3(root: Path) -> list[Finding]:
    out = []
    for tool in A.tool_names(root):
        logic = root / A.TOOLS / tool / "handler" / "logic.py"
        rel_schema = f"{A.TOOLS}/{tool}/schema.json"
        if not logic.is_file() or not (root / rel_schema).is_file():
            continue
        rel = logic.relative_to(root).as_posix()
        try:
            sig = run_signature(logic.read_text(encoding="utf-8"))
            schema = cfg.read(root, rel_schema)
        except (SyntaxError, cfg.ConfigError, NormalizationError) as exc:
            out.append(_err("C3", rel, f"cannot parse: {exc}"))
            continue
        if sig is None:
            out.append(_err("C3", rel, "no run() function"))
            continue
        params, required = sig
        props = list((schema.get("properties") or {}))
        if sorted(params) != sorted(props):
            out.append(_err("C3", rel, f"run() parameters {params} differ from schema properties {props}"))
        if sorted(required) != sorted(schema.get("required") or []):
            out.append(_err("C3", rel, f"run() required parameters {required} differ from schema required "
                                       f"{schema.get('required')}"))
    return out


def c6(root: Path) -> list[Finding]:
    out = []
    metas = _tools(root)
    for tool in A.tool_names(root):
        rel = f"{A.TOOLS}/{tool}/permissions.json"
        if not (root / rel).is_file():
            continue
        perms = _read_json(root, rel, "C6", out)
        if perms is None:
            continue
        if not isinstance(perms, list):
            out.append(_err("C6", rel, "must be a list"))
            continue
        if metas.get(tool, {}).get("kind") == "delegate" and perms:
            out.append(_err("C6", rel, "a delegate tool has no permissions ([])"))
        for entry in perms:
            ref = entry.get("resource") if isinstance(entry, dict) else None
            if ref not in REF_ACTIONS:
                out.append(_err("C6", rel, f"unknown resource ref {ref!r}"))
                continue
            for action in entry.get("actions") or []:
                if "*" in action:
                    out.append(_err("C6", rel, f"wildcard action {action!r}"))
                elif action not in REF_ACTIONS[ref]:
                    out.append(_err("C6", rel, f"action {action!r} is not allowed on {ref}"))
    return out


def c10(root: Path) -> list[Finding]:
    return [_err("C10", f"{A.TOOLS}/{t}/tool.json", "mutating tool must define shadowResult")
            for t, meta in _tools(root).items() if meta.get("mutating") and not meta.get("shadowResult")]


# ---------------------------------------------------------------- C4, C5, C7, C8, C9
def _prompts(root: Path) -> dict[str, str]:
    return {a: (root / A.AGENT / "prompts" / f"{a}.md").read_text(encoding="utf-8") for a in A.agents(root)}


def c4_c5(root: Path) -> list[Finding]:
    out = []
    offers = offered(_tools(root))
    try:
        allow = set(cfg.orchestration(root).get("allowUnreferenced") or [])
    except (cfg.ConfigError, NormalizationError):
        allow = set()
    for agent, text in _prompts(root).items():
        rel = f"{A.AGENT}/prompts/{agent}.md"
        refs = set(TOOL_REF.findall(text))
        tools = offers.get(agent, set())
        for name in sorted(refs - tools):
            out.append(_err("C4", rel, f"instruction references a tool the agent is not offered: {name}"))
        for name in sorted(tools - refs - allow):
            out.append(_err("C5", rel, f"offered but never referenced: {name}"))
    return out


def c7_c8(root: Path) -> list[Finding]:
    out = []
    try:
        models = cfg.models(root)
        orch = cfg.orchestration(root)
    except (cfg.ConfigError, NormalizationError) as exc:
        return [_err("C7", getattr(exc, "path", ""), str(exc))]
    variables = set((orch.get("promptVars") or {}))
    for agent, text in _prompts(root).items():
        rel = f"{A.AGENT}/prompts/{agent}.md"
        budget = (models.get(agent) or {}).get("promptBudgetTokens")
        if budget is None:
            out.append(_err("C7", rel, f"models.yaml has no promptBudgetTokens for {agent}"))
        elif len(text) / 4 > budget:
            out.append(_err("C7", rel, f"prompt is ~{len(text) // 4} tokens, over the {budget} budget"))
        for var in sorted(set(PROMPT_VAR.findall(text)) - variables):
            out.append(_err("C8", rel, f"{{{{{var}}}}} has no value in orchestration.yaml promptVars"))
    return out


def c9(root: Path) -> list[Finding]:
    out = []
    rel = A.SINGLE["flags"]
    flags = (_read_json(root, rel, "C9", out) or {}).get("flags", {})
    for tool, meta in _tools(root).items():
        key = meta.get("requiresFlag")
        if key and key not in flags:
            out.append(_err("C9", f"{A.TOOLS}/{tool}/tool.json", f"requiresFlag {key!r} is not in flags.json"))
    for key, flag in flags.items():
        for target in flag.get("contextTargets", []):
            if target.get("contextKind") == "team":
                for team in sorted(set(target.get("values", [])) - set(KNOWN_TEAMS)):
                    out.append(_err("C9", rel, f"flag {key} targets unknown team {team!r}"))
    return out


# ---------------------------------------------------------------- C11
def guardrail_policy_fields(root: Path) -> list[str]:
    """GuardrailPolicy's annotated fields, read with ast (Module 5 is never imported, M13)."""
    tree = ast.parse((root / GUARDRAIL_POLICY_SRC).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GuardrailPolicy":
            return [s.target.id for s in node.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
    raise LookupError("class GuardrailPolicy not found")


def c11(root: Path) -> list[Finding]:
    rel = A.SINGLE["guardrail"]
    try:
        doc = cfg.guardrail(root)
        allowed = set(guardrail_policy_fields(root)) | GUARDRAIL_EXTRA_KEYS
    except (cfg.ConfigError, NormalizationError, LookupError, OSError, SyntaxError) as exc:
        return [_err("C11", rel, str(exc))]
    out = [_err("C11", rel, f"{k} is not a GuardrailPolicy field") for k in sorted(set(doc) - allowed)]
    try:
        from . import guardrail as projection
    except ImportError:
        return out
    try:
        projection.definition_sha256(doc)
    except Exception as exc:  # noqa: BLE001 - any projection failure is a finding
        out.append(_err("C11", rel, f"guardrail_projection() failed: {exc}"))
    return out


# ---------------------------------------------------------------- C12
def c12(root: Path) -> list[Finding]:
    out = [_err("C12", f"{closures.CLOSURES_DIR}/{name}", "closure stale: run `module11 closure update`")
           for name in closures.stale(root)]
    out += [_err("C12", f"{closures.CLOSURES_DIR}/edge.txt", p) for p in closures.edge_problems(root)]
    try:
        from . import router_spec
    except ImportError:
        return out
    check = getattr(router_spec, "js_constants_current", None)
    if check is not None and not check(root):
        out.append(_err("C12", "module11/edge/lambda/router.mjs",
                        "router constants stale: run `module11 gen router-consts`"))
    return out


# ---------------------------------------------------------------- C13
def codeowners_file(root: Path) -> tuple[str, str] | None:
    for rel in CODEOWNERS_PATHS:
        if (root / rel).is_file():
            return rel, (root / rel).read_text(encoding="utf-8")
    return None


def _files_under(root: Path, rel_dir: str) -> list[str]:
    base = root / rel_dir
    if not base.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in base.rglob("*")
                  if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts and "venv" not in p.parts)


def c13_paths(root: Path) -> list[str]:
    paths = set(A.compute(root).sources("behavior")) if (root / A.HARNESS).is_dir() else set()
    for d in C13_DIRS:
        paths |= set(_files_under(root, d))
    for name in ("__init__.py", "cli.py", "__main__.py"):
        if (root / "module11" / name).is_file():
            paths.add(f"module11/{name}")
    paths |= {p.relative_to(root).as_posix() for p in (root / "module11").glob("requirements*")} \
        if (root / "module11").is_dir() else set()
    paths |= {p.relative_to(root).as_posix() for p in (root / "tests").glob("test_module11_*")} \
        if (root / "tests").is_dir() else set()
    paths |= set(_files_under(root, "tests/fixtures/module11"))
    paths |= set(PLATFORM_PATHS)
    return sorted(paths)


def _is_quality_path(path: str) -> bool:
    return (any(path == d or path.startswith(d + "/") for d in QUALITY_DIRS)
            or path.startswith("tests/test_module11_") or path.startswith("tests/fixtures/module11/"))


def c13(root: Path, text: str | None = None, rel: str | None = None) -> list[Finding]:
    if text is None:
        found = codeowners_file(root)
        if found is None:
            return [_err("C13", CODEOWNERS_PATHS[0], "no CODEOWNERS file")]
        rel, text = found
    sections = codeowners.parse(text)
    out = []
    for path in c13_paths(root):
        owners = codeowners.owners_for(sections, path)
        if not owners:
            out.append(_err("C13", path, f"no code owner for {path}"))
            continue
        if _is_quality_path(path):
            quality = [n for n, o in owners.items() if {codeowners.owner_leaf(x) for x in o} == {"quality"}
                       or (n == codeowners.QUALITY_SECTION and o and all(codeowners.is_user(x) for x in o))]
            peer = [n for n, o in owners.items() if n not in quality and o
                    and {codeowners.owner_leaf(x) for x in o} <= {"harness-owners", "platform"}]
            if not quality or not peer:
                out.append(_err("C13", path, f"quality path {path} needs a quality owner and a peer section"))
        if path in PLATFORM_PATHS and "Platform" not in owners:
            out.append(_err("C13", path, f"{path} must match a [Platform] line"))
    return out


# ---------------------------------------------------------------- C14
def c14(root: Path, rows: list[dict] | None = None) -> list[Finding]:
    rel = cfg.SCENARIO_DIR
    if rows is None:
        try:
            rows = cfg.scenarios(root)
        except (cfg.ConfigError, NormalizationError) as exc:
            return [_err("C14", getattr(exc, "path", rel), str(exc))]
    fast = [r for r in rows if "fast" in (r.get("tags") or [])]
    out = []
    if len(fast) != 12:
        out.append(_err("C14", rel, f"{len(fast)} scenarios are tagged fast; exactly 12 are required"))
    for category in cfg.CATEGORIES:
        n = sum(1 for r in fast if r.get("category") == category)
        if n < 2:
            out.append(_err("C14", rel, f"fast subset has {n} {category} scenarios; at least 2 are required"))
    if "incident-triage-014" not in {r.get("scenario_id") for r in fast}:
        out.append(_err("C14", rel, "fast subset must include incident-triage-014"))
    return out


# ---------------------------------------------------------------- C15
def ci_image_tag(root: Path) -> str:
    """First 16 hex characters of the hash of the CI image inputs (M3)."""
    hashes = {}
    for rel in CI_IMAGE_INPUTS:
        path = root / rel
        files = _files_under(root, rel) if path.is_dir() else ([rel] if path.is_file() else [])
        for f in files:
            hashes[f] = file_hash(f, (root / f).read_bytes())
    return hex_of(files_hash(hashes))[:16]


def c15(root: Path) -> list[Finding]:
    rel = next((p for p in CI_PATHS if (root / p).is_file()), None)
    if rel is None:
        return [_err("C15", CI_PATHS[0], "no .gitlab-ci.yml")]
    try:
        doc = yaml.safe_load((root / rel).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [_err("C15", rel, f"invalid YAML: {exc}")]
    out = []
    declared = str((doc.get("variables") or {}).get("M11_CI_IMAGE", ""))
    if isinstance((doc.get("variables") or {}).get("M11_CI_IMAGE"), dict):
        declared = str(doc["variables"]["M11_CI_IMAGE"].get("value", ""))
    expected = ci_image_tag(root)
    if declared != expected:
        out.append(_err("C15", rel, f"M11_CI_IMAGE is {declared!r}; the CI image inputs hash to {expected!r}"))
    for name, job in doc.items():
        if not (isinstance(job, dict) and name.startswith("promote:")):
            continue
        image = job.get("image")
        ref = image.get("name") if isinstance(image, dict) else image
        if not ref or "@sha256:" not in str(ref) or "CI_REGISTRY_IMAGE" in str(ref):
            out.append(_err("C15", rel, f"{name} must use an image pinned by @sha256: digest outside $CI_REGISTRY_IMAGE"))
    return out


# ---------------------------------------------------------------- C16
def c16(plan: D.ChangePlan | None, rules, unknown, has_base: bool = True, own_kinds: Iterable[str] | None = None,
        commit_range: str | None = None) -> list[Finding]:
    if plan is None or not D.c16_violation(plan, has_base):
        return []
    return [_err("C16", "change plan", "ChangePlanRejected: " + D.c16_message(plan, rules, unknown, own_kinds, commit_range))]


# ---------------------------------------------------------------- groups
def groups_disjoint(direct: Mapping[str, Iterable[str]], inherited: Mapping[str, Iterable[str]] | None = None) -> list[Finding]:
    """`quality` must share no member with `harness-owners` or `platform`. `direct` comes from
    GET /groups/:id/members (what CODEOWNERS counts); `inherited` from /members/all."""
    out = []
    for label, groups, severity in (("direct", direct, "error"), ("inherited", inherited or {}, "warning")):
        quality = set(groups.get("quality", []))
        for other in ("harness-owners", "platform"):
            both = sorted(quality & set(groups.get(other, [])))
            if both:
                out.append(Finding("GROUPS", f"quality/{other}",
                                   f"{label} members in both quality and {other}: {', '.join(both)}", severity))
    return out


# ---------------------------------------------------------------- all
def run(repo_root: str | Path, plan: D.ChangePlan | None = None, has_base: bool = True,
        own_kinds: Iterable[str] | None = None, commit_range: str | None = None) -> list[Finding]:
    root = Path(repo_root)
    findings: list[Finding] = []
    for fn in (c1, c2, c3, c4_c5, c6, c7_c8, c9, c10, c11, c12, c13, c14, c15):
        findings += fn(root)
    if plan is not None:
        rules, unknown, _ = cfg.change_types(root)
        findings += c16(plan, rules, unknown, has_base, own_kinds, commit_range)
    return findings


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


def to_json(findings: Iterable[Finding]) -> str:
    return json.dumps([f.__dict__ for f in findings], indent=2)
