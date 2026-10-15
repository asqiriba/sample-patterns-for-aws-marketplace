# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Runtime configuration: the `M11_RUNTIME_CONFIG` env value and the bundle component.

`M11_RUNTIME_CONFIG` holds runtime-shaping fields only; release identity travels in the envelope.
It is validated at process start; on failure the process exits 1, so `/ping` never
turns healthy. `bundleArn: null` with `bundleVersion: null` is bootstrap mode (M5).

The bundle component (`m11.bundle/2`) is the model-facing configuration. A W3C `baggage`
header carrying `aws.agentcore.configbundle_arn` and `aws.agentcore.configbundle_version` wins
over the env pin (only Gateway A/B tests set it); otherwise the env pin applies. Versions
are immutable, so fetched components are cached by version ID and never invalidated.
"""
from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

BUNDLE_SCHEMA_ID = "m11.bundle/2"
ENV_NAME = "M11_RUNTIME_CONFIG"
BAGGAGE_ARN = "aws.agentcore.configbundle_arn"
BAGGAGE_VERSION = "aws.agentcore.configbundle_version"
AGENT_NAMES = {"companion": "DevOps Companion", "planner": "Recovery Planner"}

_SHA = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_REF = {"type": "object", "required": ["uri", "sha256"], "additionalProperties": False,
        "properties": {"uri": {"type": "string", "minLength": 1}, "sha256": _SHA}}
RUNTIME_CONFIG_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["stage", "stagekey", "bundleArn", "bundleVersion", "toolArns", "guardrail", "flagsKey",
                 "artifactsBucket", "snapshot", "evidence", "memoryId", "runtimeDigest"],
    "properties": {
        "stage": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"},
        "stagekey": {"type": "string", "pattern": "^[a-z0-9-]{1,40}$"},
        "bundleArn": {"type": ["string", "null"], "minLength": 1},
        "bundleVersion": {"type": ["string", "null"], "minLength": 1},
        "toolArns": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
        "guardrail": {"type": "object", "required": ["id", "version"], "additionalProperties": False,
                      "properties": {"id": {"type": "string", "minLength": 1}, "version": {"type": "string", "minLength": 1}}},
        "flagsKey": {"type": "string", "minLength": 1},
        "artifactsBucket": {"type": "string", "minLength": 1},
        "snapshot": _REF, "evidence": _REF,
        "memoryId": {"type": "string", "minLength": 1},
        "runtimeDigest": _SHA,
    },
}

_MODEL = {"type": "object", "required": ["modelId", "maxOutputTokens"],
          "properties": {"modelId": {"type": "string", "minLength": 1},
                         "maxOutputTokens": {"type": "integer", "minimum": 1, "maximum": 32000},
                         "reasoningEffort": {"type": "string"}}}
_TOOL = {"type": "object", "required": ["meta", "schema", "description"], "additionalProperties": False,
         "properties": {
             "meta": {"type": "object", "required": ["name", "agents", "kind", "mutating"],
                      "properties": {"name": {"type": "string"}, "agents": {"type": "array", "items": {"enum": ["companion", "planner"]}},
                                     "kind": {"enum": ["lambda", "delegate"]}, "mutating": {"type": "boolean"},
                                     "requiresFlag": {"type": "string"}, "shadowResult": {"type": "object"},
                                     "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 30}}},
             "schema": {"type": "object", "required": ["type", "properties"]},
             "description": {"type": "string", "minLength": 1}}}
BUNDLE_COMPONENT_SCHEMA = {
    "type": "object", "required": ["schema", "prompts", "tools", "models", "orchestration"],
    "properties": {
        "schema": {"const": BUNDLE_SCHEMA_ID},
        "prompts": {"type": "object", "required": ["companion", "planner"],
                    "additionalProperties": {"type": "string", "minLength": 1}},
        "tools": {"type": "object", "minProperties": 1, "additionalProperties": _TOOL},
        "models": {"type": "object", "required": ["companion", "planner"], "additionalProperties": _MODEL},
        "orchestration": {"type": "object", "required": ["maxTurns", "maxToolCalls", "deadlineSeconds", "requiredTools"],
                          "properties": {"maxTurns": {"type": "integer", "minimum": 1},
                                         "maxToolCalls": {"type": "integer", "minimum": 1},
                                         "deadlineSeconds": {"type": "integer", "minimum": 1, "maximum": 900},
                                         "requiredTools": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
                                         "promptVars": {"type": "object"}}},
    },
}


class ConfigInvalid(ValueError):
    """Runtime config or bundle component failed validation (invocation answers 500 `config_invalid`)."""


def _validate(doc: Any, schema: dict, what: str) -> None:
    import jsonschema
    try:
        jsonschema.validate(doc, schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise ConfigInvalid(f"{what}: {where}: {exc.message}") from None


@dataclass(frozen=True)
class RuntimeConfig:
    raw: dict

    @property
    def bootstrap(self) -> bool:
        return self.raw["bundleArn"] is None

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def data_context(self) -> dict:
        """`context.data` for every tool call (M6): each arm reads its own pinned data."""
        return {"snapshot": dict(self.raw["snapshot"]), "evidence": dict(self.raw["evidence"]),
                "memoryId": self.raw["memoryId"], "artifactsBucket": self.raw["artifactsBucket"]}


def parse_runtime_config(text: str | None) -> RuntimeConfig:
    if not text:
        raise ConfigInvalid(f"{ENV_NAME} is not set")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ConfigInvalid(f"{ENV_NAME} is not JSON: {exc}") from None
    _validate(doc, RUNTIME_CONFIG_SCHEMA, ENV_NAME)
    if (doc["bundleArn"] is None) != (doc["bundleVersion"] is None):
        raise ConfigInvalid(f"{ENV_NAME}: bundleArn and bundleVersion must both be null (bootstrap) or both set")
    return RuntimeConfig(doc)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    agents: tuple[str, ...]
    kind: str
    mutating: bool
    schema: dict
    description: str
    requires_flag: str | None = None
    shadow_result: dict | None = None
    timeout_seconds: int = 10

    def definition(self) -> dict:
        """The Responses API function definition (strict, as Module 10's Tool.definition)."""
        parameters = {k: v for k, v in self.schema.items() if k != "$schema"}
        return {"type": "function", "name": self.name, "description": self.description,
                "strict": True, "parameters": parameters}


@dataclass(frozen=True)
class Bundle:
    version: str
    component: dict
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    @property
    def orchestration(self) -> dict:
        return self.component["orchestration"]

    def prompt(self, agent: str) -> str:
        text = self.component["prompts"][agent]
        for name, value in (self.orchestration.get("promptVars") or {}).items():
            text = text.replace("{{" + name + "}}", str(value))
        return text

    def model(self, agent: str) -> dict:
        return self.component["models"][agent]

    def required_tools(self, agent: str) -> set[str]:
        return set(self.orchestration["requiredTools"].get(agent, []))

    def tools_for(self, agent: str) -> list[ToolSpec]:
        return [spec for name, spec in sorted(self.tools.items()) if agent in spec.agents]


def load_component(component: Any, version: str) -> Bundle:
    """Validate an `m11.bundle/2` component with jsonschema and derive the tool specs."""
    _validate(component, BUNDLE_COMPONENT_SCHEMA, "bundle component")
    tools = {}
    for name, docs in component["tools"].items():
        meta = docs["meta"]
        if meta["name"] != name:
            raise ConfigInvalid(f"bundle component: tools/{name}: meta.name is {meta['name']!r}")
        if meta["mutating"] and not isinstance(meta.get("shadowResult"), dict):
            raise ConfigInvalid(f"bundle component: tools/{name}: a mutating tool needs shadowResult")
        tools[name] = ToolSpec(name=name, agents=tuple(meta["agents"]), kind=meta["kind"], mutating=meta["mutating"],
                               schema=docs["schema"], description=docs["description"].strip(),
                               requires_flag=meta.get("requiresFlag"), shadow_result=meta.get("shadowResult"),
                               timeout_seconds=int(meta.get("timeoutSeconds", 10)))
    return Bundle(version=version, component=copy.deepcopy(component), tools=tools)


def resolve_bundle(config: RuntimeConfig, baggage: dict[str, str]) -> tuple[str, str, bool]:
    """(bundle ARN, version, override). Baggage wins only when it names both entries;
    the service's own `Self` and `session.id` baggage entries are ignored."""
    arn, version = baggage.get(BAGGAGE_ARN), baggage.get(BAGGAGE_VERSION)
    if arn and version:
        return arn, version, True
    return config["bundleArn"], config["bundleVersion"], False


def _local_dir(arn: str) -> Path | None:
    parts = urlsplit(arn)
    return Path(unquote(parts.path)) if parts.scheme == "file" else None


def _fetch_version(arn: str, version: str) -> dict:
    """GetConfigurationBundleVersion (bundleId is the ARN's last segment), or `<dir>/<version>.json`
    for a `file://` bundle in local runs."""
    local = _local_dir(arn)
    if local is not None:
        path = local / f"{version}.json"
        if not path.is_file() or "/" in version or "\\" in version:
            raise ConfigInvalid(f"local bundle version {version!r} not found")
        return json.loads(path.read_text(encoding="utf-8"))
    from module11.aws import client
    return client("bedrock-agentcore-control").get_configuration_bundle_version(
        bundleId=arn.rsplit("/", 1)[-1], versionId=version)


class BundleSource:
    """Fetch-and-cache by version ID (versions are immutable)."""

    def __init__(self, fetch: Callable[[str, str], dict] | None = None) -> None:
        self._fetch = fetch or _fetch_version
        self._cache: dict[tuple[str, str], Bundle] = {}
        self._lock = threading.Lock()
        self.fetches = 0

    def get(self, arn: str, version: str) -> Bundle:
        key = (arn, version)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        response = self._fetch(arn, version)
        self.fetches += 1
        components = response.get("components") or {}
        if len(components) != 1:
            raise ConfigInvalid(f"bundle version {version} has {len(components)} components (expected 1)")
        configuration = next(iter(components.values())).get("configuration")
        bundle = load_component(configuration, version)
        with self._lock:
            self._cache[key] = bundle
        return bundle
