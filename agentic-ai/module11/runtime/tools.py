# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tool dispatch, the side-effect wrapper, and the in-process recovery delegate.

`ToolClient.call(name, args, ctx)`:
- a mutating tool in a shadow session returns the tool's `shadowResult` rendered with
  `{note_id: "shadow-<uuid4>", shadow_suppressed: true}` and never invokes Lambda; the caller
  marks the `execute_tool` span, the streamed tool event and the result `events[]` entry;
- every other Lambda tool is invoked by qualified ARN with `context.data` copied from the
  runtime config (M6) and the envelope's identity fields.

`local:<tool>` ARNs (loopback local runs only) call the repo's handler in-process.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


from .config import ToolSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "module11" / "harness" / "agent" / "tools"
LOCAL_PREFIX = "local:"


@dataclass
class CallContext:
    """What a tool call carries: identity from the envelope, data locations from the config."""
    stage: str
    manifest_version: str
    behavior_digest: str
    release: str
    variant: str
    session_id: str
    conversation_id: str
    request_id: str
    team: str
    shadow: bool
    data: dict
    sequence: int = 0

    def event_context(self) -> dict:
        return {"stage": self.stage, "manifestVersion": self.manifest_version, "behaviorDigest": self.behavior_digest,
                "variant": self.variant, "sessionId": self.session_id, "conversationId": self.conversation_id,
                "requestId": self.request_id, "team": self.team, "release": self.release, "shadow": self.shadow,
                "sequence": self.sequence, "data": self.data}


class LambdaInvoker:
    """Synchronous Lambda invoke of a qualified ARN, without SDK retries (a retried mutating call
    would write twice)."""

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _lambda(self):
        if self._client is None:
            from botocore.config import Config
            from module11.aws import client
            self._client = client("lambda", config=Config(read_timeout=40, retries={"total_max_attempts": 1}))
        return self._client

    def invoke(self, arn: str, event: dict, timeout_seconds: int) -> dict:
        response = self._lambda().invoke(FunctionName=arn, InvocationType="RequestResponse",
                                         Payload=json.dumps(event).encode("utf-8"))
        payload = json.loads(response["Payload"].read() or b"{}")
        if response.get("FunctionError"):
            return {"ok": False, "error": {"code": "internal", "message": "tool function error"}}
        return payload


class LocalInvoker:
    """`local:<tool>` -> the handler under module11/harness/agent/tools/<tool>/handler/index.py,
    loaded in-process. Each handler imports its own `logic`; the module cache entry is swapped
    while loading so seven handlers can coexist in one process."""

    def __init__(self, tools_dir: Path = TOOLS_DIR) -> None:
        self.tools_dir = tools_dir
        self._handlers: dict[str, Callable] = {}
        self._lock = threading.Lock()

    def _load(self, tool: str) -> Callable:
        with self._lock:
            if tool not in self._handlers:
                index = self.tools_dir / tool / "handler" / "index.py"
                if not index.is_file():
                    raise LookupError(f"no local handler for {tool}")
                saved = sys.modules.pop("logic", None)
                try:
                    # Load this handler's logic.py first, so index.py's `import logic` binds it
                    # whatever other handler directories are already on sys.path.
                    logic_spec = importlib.util.spec_from_file_location("logic", index.parent / "logic.py")
                    logic = importlib.util.module_from_spec(logic_spec)
                    sys.modules["logic"] = logic
                    logic_spec.loader.exec_module(logic)
                    spec = importlib.util.spec_from_file_location(f"m11_local_handler_{tool}", index)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop("logic", None)
                    if saved is not None:
                        sys.modules["logic"] = saved
                self._handlers[tool] = module.handler
            return self._handlers[tool]

    def invoke(self, arn: str, event: dict, timeout_seconds: int) -> dict:
        if not arn.startswith(LOCAL_PREFIX):
            raise ValueError("LocalInvoker accepts only local:<tool> ARNs")
        return self._load(arn[len(LOCAL_PREFIX):])(json.loads(json.dumps(event)), None)


class RoutingInvoker:
    """Lambda for real ARNs, in-process for `local:` ones (allowed only in a loopback local run)."""

    def __init__(self, allow_local: bool, lambda_invoker: Any = None, local_invoker: Any = None) -> None:
        self.allow_local = allow_local
        self._lambda = lambda_invoker or LambdaInvoker()
        self._local = local_invoker or LocalInvoker()

    def invoke(self, arn: str, event: dict, timeout_seconds: int) -> dict:
        if arn.startswith(LOCAL_PREFIX):
            if not self.allow_local:
                raise ValueError("local tool ARNs are refused outside a local run")
            return self._local.invoke(arn, event, timeout_seconds)
        return self._lambda.invoke(arn, event, timeout_seconds)


@dataclass
class ToolOutcome:
    ok: bool
    result: dict | None = None
    error: dict | None = None
    shadow_suppressed: bool = False

    def for_model(self) -> dict:
        return self.result if self.ok else {"error": self.error}


@dataclass
class ToolClient:
    specs: dict[str, ToolSpec]
    tool_arns: dict[str, str]
    invoker: Any
    calls: list[dict] = field(default_factory=list)

    def call(self, name: str, args: dict, ctx: CallContext) -> ToolOutcome:
        spec = self.specs[name]
        if spec.kind != "lambda":
            raise ValueError(f"{name} is not a Lambda tool")
        ctx.sequence += 1
        if spec.mutating and ctx.shadow:
            rendered = {**(spec.shadow_result or {}), "note_id": f"shadow-{uuid.uuid4()}", "shadow_suppressed": True}
            return ToolOutcome(True, rendered, shadow_suppressed=True)
        arn = self.tool_arns.get(name)
        if not arn:
            return ToolOutcome(False, error={"code": "internal", "message": f"no function is pinned for {name}"})
        event = {"arguments": args, "context": ctx.event_context()}
        self.calls.append({"tool": name, "arn": arn, "event": event})
        try:
            payload = self.invoker.invoke(arn, event, spec.timeout_seconds)
        except Exception as exc:
            return ToolOutcome(False, error={"code": "internal", "message": type(exc).__name__})
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            return ToolOutcome(False, error={"code": "internal", "message": "malformed tool response"})
        if payload["ok"]:
            result = payload.get("result")
            return ToolOutcome(True, result if isinstance(result, dict) else {"value": result})
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {"code": "internal"}
        return ToolOutcome(False, error={"code": str(error.get("code", "internal")),
                                         "message": str(error.get("message", ""))[:200]})


DELEGATE = "delegate_recovery_planning"


GUARD_BLOCKED = "guard_blocked"
GUARD_MESSAGE = "call get_deployment_history first: recovery planning needs the deployment evidence"


class RecoveryDelegate:
    """`delegate_recovery_planning`, in-process. It refuses unless `get_deployment_history` has
    completed in this run (Module 10's inspect-before-delegate guard) and hands the planner
    `{request, deployment_evidence, metrics}`. The plan is a draft for human review: it returns
    `requires_human_review=True, changes_executed=False`, mirroring module10.recovery.investigate().

    An out-of-order call is refused with a recoverable tool error (`guard_blocked`), so the model can gather the
    deployment history and ask again; the planner never runs before that evidence exists. The attempt stays in the
    trace as a failed `execute_tool` span (ExactOrder scenarios still fail on it), and a second out-of-order
    attempt ends the session (runtime/loop.py)."""

    def __init__(self, run_planner: Callable[[str], str]) -> None:
        self.run_planner = run_planner

    def __call__(self, args: dict, change_summary: str, results: dict[str, dict]) -> ToolOutcome:
        if "get_deployment_history" not in results:
            return ToolOutcome(False, error={"code": GUARD_BLOCKED, "message": GUARD_MESSAGE})
        task = json.dumps({"request": change_summary, "deployment_evidence": results["get_deployment_history"],
                           "metrics": results.get("query_metrics")}, sort_keys=True)
        plan = self.run_planner(task)
        return ToolOutcome(True, {"summary": "Recovery Planner completed a draft for human review.",
                                  "draft_plan": plan, "requires_human_review": True, "changes_executed": False})
