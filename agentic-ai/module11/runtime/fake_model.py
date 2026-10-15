# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A scripted stand-in for the Bedrock Responses API: offline tests and the loopback local run
(`python -m module11.runtime.app --fake-model`). It is never used with `--agentcore`.

It answers with objects shaped like the OpenAI SDK's (`output` items with `type`, `name`,
`arguments`, `call_id`, `model_dump()`; `output_text`; `usage.input_tokens_details`), and follows
the companion prompt's order: history first, then metrics, runbooks and logs, the delegate when a
plan is requested and offered, the incident note, then a short diagnosis that names the
deployment, quotes one log line and ends with one `Proposal, not executed:` step.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

SERVICES = ("checkout-service", "payments-service", "reporting-service")


@dataclass
class FakeItem:
    type: str
    name: str | None = None
    arguments: str | None = None
    call_id: str | None = None
    text: str | None = None
    id: str = "item-fake"

    def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict:
        if self.type == "function_call":
            data = {"type": "function_call", "id": self.id, "call_id": self.call_id, "name": self.name,
                    "arguments": self.arguments, "status": "completed"}
        else:
            data = {"type": "message", "id": self.id, "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": self.text or "", "annotations": []}]}
        return {k: v for k, v in data.items() if v is not None} if exclude_none else data


@dataclass
class FakeDetails:
    cached_tokens: int = 0


@dataclass
class FakeUsage:
    input_tokens: int = 120
    output_tokens: int = 40
    cached: int = 64
    input_tokens_details: FakeDetails = field(default_factory=FakeDetails)

    def __post_init__(self) -> None:
        self.input_tokens_details = FakeDetails(self.cached)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def model_dump(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens, "input_tokens_details": {"cached_tokens": self.cached}}


@dataclass
class FakeResponse:
    output: list[FakeItem]
    model: str = "fake-model"
    status: str = "completed"
    id: str = "resp-fake"
    usage: FakeUsage | None = field(default_factory=FakeUsage)

    @property
    def output_text(self) -> str:
        return "".join(i.text or "" for i in self.output if i.type == "message")


def call(name: str, arguments: dict, n: int) -> FakeItem:
    return FakeItem("function_call", name=name, arguments=json.dumps(arguments), call_id=f"call-{name}-{n}",
                    id=f"fc-{name}-{n}")


def text(value: str) -> FakeResponse:
    return FakeResponse([FakeItem("message", text=value)])


def _outputs(history: list[dict]) -> dict[str, dict]:
    names = {i.get("call_id"): i.get("name") for i in history if i.get("type") == "function_call"}
    out = {}
    for item in history:
        if item.get("type") == "function_call_output":
            try:
                out[names.get(item.get("call_id"))] = json.loads(item.get("output") or "{}")
            except ValueError:
                pass
    return out


def service_of(task: str) -> str:
    lowered = task.lower()
    for name in SERVICES:
        if name in lowered or name.split("-")[0] in lowered:
            return name
    return SERVICES[0]


class FakeModelClient:
    """Scripted agent. `script` may replace the default policy: `script(params) -> FakeResponse`."""

    def __init__(self, script: Callable[[dict], FakeResponse] | None = None) -> None:
        self.script = script or self.default
        self.requests: list[dict] = []

    def create(self, **params: Any) -> FakeResponse:
        self.requests.append(params)
        return self.script(params)

    @staticmethod
    def default(params: dict) -> FakeResponse:
        history = params["input"]
        offered = {t["name"] for t in params.get("tools") or []}
        done = _outputs(history)
        task = history[0]["content"] if history and isinstance(history[0].get("content"), str) else ""
        n = len(history)
        if "get_blast_radius" in offered:  # the planner
            service = json.loads(task).get("deployment_evidence", {}).get("service", SERVICES[0]) if task.startswith("{") else SERVICES[0]
            if not done:
                return FakeResponse([call("get_blast_radius", {"service_name": service}, n),
                                     call("check_rollback_readiness", {"service_name": service}, n)])
            return text("### Finding\nThe latest deployment caused the incident.\n### Recovery steps\n"
                        "1. Confirm readiness. 2. Roll back after approval.\n### Approval needed\n"
                        "The release owner must approve. No changes executed.")
        service = service_of(task)
        if "get_deployment_history" not in done:
            return FakeResponse([call("get_deployment_history", {"service_name": service}, n)])
        pending = [t for t in ("query_metrics", "search_runbooks", "get_service_logs") if t in offered and t not in done]
        if pending:
            args = {"query_metrics": {"service_name": service}, "get_service_logs": {"service_name": service},
                    "search_runbooks": {"query": f"{service} {task}"[:300]}}
            return FakeResponse([call(t, args[t], n) for t in pending])
        wants_plan = re.search(r"\bplan\b|recover", task, re.IGNORECASE)
        if wants_plan and "delegate_recovery_planning" in offered and "delegate_recovery_planning" not in done:
            return FakeResponse([call("delegate_recovery_planning", {"service_name": service}, n)])
        lines = (done.get("get_service_logs") or {}).get("lines") or ["no log line returned"]
        quoted = next((line for line in lines if "ERROR" in line), lines[0])
        quoted = re.search(r'msg="([^"]+)"', quoted).group(1) if 'msg="' in quoted else quoted
        deployment = ((done.get("get_deployment_history") or {}).get("history") or [{}])[0]
        deploy_id = deployment.get("deployment_id") or deployment.get("version") or "the latest deployment"
        if "record_incident_note" in offered and "record_incident_note" not in done:
            return FakeResponse([call("record_incident_note", {
                "service_name": service, "probable_cause": f"Deployment {deploy_id} introduced the failure.",
                "log_line": quoted[:1000]}, n)])
        return text(f"The incident correlates with deployment {deploy_id}; the logs show \"{quoted}\". "
                    f"Proposal, not executed: roll back {service} after the release owner approves.")
