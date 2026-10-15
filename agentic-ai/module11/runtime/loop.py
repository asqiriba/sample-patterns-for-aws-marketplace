# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The agent loop: a parameterized descendant of Module 10's `_run_agent`.

Kept from Module 10 (`module10/agent_runner.py`): advertised-tools-only dispatch, the
required-evidence check before final text, stateless history that keeps reasoning items,
`store=False`, and `AgentRun` / `AgentRunError` themselves (imported, not copied).

Moved into files: turns, tool calls and the deadline come from `orchestration.yaml`, output
tokens and reasoning effort from `models.yaml`, required tools from `requiredTools`, and tool
arguments are validated with `jsonschema` against each tool's `schema.json`. An invalid
argument becomes a tool error result (`invalid_arguments`) and the loop continues (4.7).
Module 10's "no repeated tools" rule becomes the `maxToolCalls` budget; a mutating or delegate
tool still runs at most once per run. Cached input tokens are recorded per model call.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from module10.agent_runner import AgentRun, AgentRunError

from . import model_retry as MR
from .config import AGENT_NAMES, Bundle, ToolSpec
from .telemetry import Telemetry
from .tools import GUARD_BLOCKED, CallContext, RecoveryDelegate, ToolClient, ToolOutcome

MAX_GUARD_BLOCKS = 1          # one recoverable out-of-order delegation; the second ends the session

GEN_AI_AGENT = {"companion": "devops_companion", "planner": "recovery_planner"}


@dataclass
class HarnessRun:
    """State shared by the companion and the delegated planner within one invocation."""
    bundle: Bundle
    model_client: Any
    telemetry: Telemetry
    tool_client: ToolClient
    ctx: CallContext
    change_summary: str
    advertised: dict[str, list[ToolSpec]]
    run: AgentRun
    emit: Callable[[str, dict], None] = lambda name, data: None
    delegate: RecoveryDelegate | None = None
    tool_calls: int = 0
    cached_input_tokens: int = 0
    results: dict[str, dict] = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    model_retries: list[dict] = field(default_factory=list)   # every model-call retry of the invocation


class BedrockModelClient:
    """Direct Bedrock Responses API through the OpenAI SDK's Bedrock provider, the same client as
    Module 10's `BedrockReviewer`. No Portkey: the model ID is a manifest field.
    Provider 5xx and throttles are retried up to twice inside the session deadline (runtime/model_retry.py).
    `openai` is imported on first use, so the module imports without it (M13)."""

    def __init__(self) -> None:
        self._client = None

    def create(self, **params: Any) -> Any:
        if self._client is None:
            # Region and credentials come from the standard AWS chain (the runtime role in AgentCore).
            self._client = MR.client(timeout=60.0)
        return self._client.responses.create(**params)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


def _schema_error(schema: dict, arguments: Any) -> str | None:
    import jsonschema
    validator = jsonschema.Draft202012Validator({k: v for k, v in schema.items() if k != "$schema"})
    error = next(iter(sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))), None)
    if error is None:
        return None
    where = "/".join(str(p) for p in error.absolute_path) or "(arguments)"
    return f"{where}: {error.message}"[:300]


def _messages(history: list[dict]) -> list[dict]:
    """History rendered as GenAI semantic-convention messages for the chat span."""
    out = []
    for item in history:
        kind = item.get("type")
        if item.get("role") == "user":
            out.append({"role": "user", "parts": [{"type": "text", "content": item.get("content", "")}]})
        elif kind == "function_call":
            out.append({"role": "assistant", "parts": [{"type": "tool_call", "id": item.get("call_id"),
                                                       "name": item.get("name"), "arguments": item.get("arguments")}]})
        elif kind == "function_call_output":
            out.append({"role": "tool", "parts": [{"type": "tool_call_response", "id": item.get("call_id"),
                                                  "response": item.get("output")}]})
        elif kind == "message":
            text = "".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
            out.append({"role": "assistant", "parts": [{"type": "text", "content": text}]})
    return out


def _output_messages(response: Any) -> list[dict]:
    calls = [item for item in response.output if item.type == "function_call"]
    if calls:
        return [{"role": "assistant", "parts": [{"type": "tool_call", "id": c.call_id, "name": c.name,
                                                 "arguments": c.arguments} for c in calls]}]
    return [{"role": "assistant", "parts": [{"type": "text", "content": response.output_text}]}]


def _usage(response: Any) -> tuple[dict | None, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, 0
    dumped = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if cached is None and isinstance(dumped.get("input_tokens_details"), dict):
        cached = dumped["input_tokens_details"].get("cached_tokens")
    return dumped, int(cached or 0)


def _dispatch(h: HarnessRun, agent: str, specs: dict[str, ToolSpec], call: Any, once: set[str]) -> ToolOutcome:
    display = AGENT_NAMES[agent]
    event = {"agent": display, "tool": call.name, "status": "started"}
    h.run.events.append(event)
    try:
        if call.name not in specs:
            raise AgentRunError(f"{display} requested unavailable capability: {call.name}")
        spec = specs[call.name]
        if (spec.mutating or spec.kind == "delegate") and call.name in once:
            raise AgentRunError(f"{display} attempted to repeat {call.name}")
        h.tool_calls += 1
        if h.tool_calls > int(h.bundle.orchestration["maxToolCalls"]):
            raise AgentRunError(f"{display} exceeded the tool-call limit")
        with h.telemetry.execute_tool(call.name, call.arguments, call.call_id) as span:
            try:
                arguments = json.loads(call.arguments)
                problem = _schema_error(spec.schema, arguments)
            except (TypeError, ValueError):
                arguments, problem = None, "arguments are not JSON"
            if problem:
                outcome = ToolOutcome(False, error={"code": "invalid_arguments", "message": problem})
            elif spec.kind == "delegate":
                if h.delegate is None:
                    raise AgentRunError(f"{display} requested unavailable capability: {call.name}")
                outcome = h.delegate(arguments, h.change_summary, h.results)
            else:
                outcome = h.tool_client.call(call.name, arguments, h.ctx)
            span.set_attribute("gen_ai.tool.call.result", json.dumps(outcome.for_model(), sort_keys=True, default=str))
            span.set_attribute("m11.tool_status", "completed" if outcome.ok else "failed")
            if outcome.shadow_suppressed:
                span.set_attribute("m11.shadow_suppressed", True)
    except Exception:
        event["status"] = "failed"
        h.emit("tool", dict(event))
        raise
    if outcome.ok:
        once.add(call.name)
        h.results[call.name] = outcome.result
        event["status"] = "completed"
        event["summary"] = str((outcome.result or {}).get("summary", "Completed"))
    else:
        event["status"] = "failed"
        event["error"] = outcome.error.get("code", "internal")
        h.failures.append({"tool": call.name, "code": event["error"]})
    if outcome.shadow_suppressed:
        event["shadow_suppressed"] = True
    h.emit("tool", dict(event))
    blocked = sum(1 for f in h.failures if f["code"] == GUARD_BLOCKED)
    if blocked > MAX_GUARD_BLOCKS:
        raise AgentRunError(f"{display} requested recovery planning before deployment history {blocked} times")
    return outcome


class ModelUnavailable(AgentRunError):
    """A model call failed at the provider (5xx, throttle or connection) after its bounded retries. The
    Companion still ends with a plain answer that names the failure."""


def _create(h: HarnessRun, display: str, chat: Any, **params: Any) -> Any:
    """One model call with its retry budget (runtime/model_retry.py); the retries go on the chat span."""
    with MR.budget(h.run.deadline) as attempts:
        try:
            return h.model_client.create(**params)
        except Exception as exc:
            failure = MR.provider_error(exc)
            if failure is None:
                raise
            name, status = failure
            stopped = " (the session deadline left no time for another)" if attempts.stopped else ""
            raise ModelUnavailable(f"{display}: the model provider failed ({name}"
                                   f"{f' HTTP {status}' if status else ''}) after {len(attempts.retries)} "
                                   f"retries{stopped}") from exc
        finally:
            h.model_retries.extend(attempts.retries)
            chat.set_attribute("m11.model_retries", len(attempts.retries))
            if attempts.retries:
                chat.set_attribute("m11.model_retry_classes", ",".join(r["class"] for r in attempts.retries))


class IncompleteAnswer(AgentRunError):
    """The Companion's session ended without a complete investigation (a missing required evidence check,
    a planner refusal, a limit). It still carries a final answer that says so plainly and names what is
    missing; the session status stays `failed` and no gate, required tool or signal changes."""

    def __init__(self, message: str, answer: str) -> None:
        super().__init__(message)
        self.answer = answer


def _missing_note(h: HarnessRun, required: set[str], completed: set[str]) -> str:
    missing = sorted(required - completed)
    failed = {f["tool"]: f["code"] for f in h.failures}
    parts = [f"{tool} ({'failed: ' + failed[tool] if tool in failed else 'not run'})" for tool in missing]
    return ("Required evidence missing: " + ", ".join(parts) + ".") if parts else ""


def incomplete_answer(h: HarnessRun, reason: str, required: set[str], completed: set[str], text: str = "") -> str:
    """The final answer for a session that could not complete: the model's own text, if any, then a plain
    statement of why, which evidence checks completed and which required ones are missing."""
    done = ", ".join(sorted(completed)) or "none"
    lines = [text.strip()] if text and text.strip() else []
    lines.append(f"This investigation is incomplete: {reason.rstrip('.')}. Evidence checks completed: {done}.")
    note = _missing_note(h, required, completed)
    if note:
        lines.append(note)
    lines.append("Treat any cause above as unverified; no change was made.")
    return "\n\n".join(lines)


def run_harness(h: HarnessRun, agent: str, task: str, guard_input: Callable[[], Any] | None = None) -> str:
    """Run one agent to final text; raises AgentRunError on any workflow violation.
    `guard_input` (the INPUT guardrail check) runs inside the agent's `invoke_agent` span.
    For the Companion every AgentRunError becomes an IncompleteAnswer that carries a final answer, which is also
    the span's `gen_ai.task.output`; the planner's errors reach the Companion unchanged."""
    display = AGENT_NAMES[agent]
    tools = h.advertised.get(agent, [])
    specs = {t.name: t for t in tools}
    required = h.bundle.required_tools(agent)
    model = h.bundle.model(agent)
    instructions = h.bundle.prompt(agent)
    max_turns = int(h.bundle.orchestration["maxTurns"])
    completed: set[str] = set()
    once: set[str] = set()
    history: list[Any] = [{"role": "user", "content": task}]
    last_text = ""
    with h.telemetry.invoke_agent(GEN_AI_AGENT[agent], task) as agent_span:
        agent_span.set_attribute("m11.tools_offered", ",".join(sorted(specs)))
        try:
            if guard_input is not None:
                guard_input()
            for _ in range(max_turns):
                remaining = h.run.deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentRunError("The investigation exceeded its time budget")
                record = {"agent": display, "response_id": None, "model": None, "status": "failed", "usage": None,
                          "call_id": uuid4().hex, "requested_model": model["modelId"], "cached_input_tokens": 0}
                h.run.responses.append(record)
                with h.telemetry.chat(model["modelId"], instructions, _messages(history)) as chat:
                    response = _create(
                        h, display, chat, model=model["modelId"], instructions=instructions, input=list(history),
                        tools=[t.definition() for t in tools],
                        reasoning={"effort": model.get("reasoningEffort", "none")},
                        max_output_tokens=int(model["maxOutputTokens"]), store=False, timeout=min(60.0, remaining))
                    usage, cached = _usage(response)
                    record.update({"response_id": response.id, "model": response.model, "status": response.status,
                                   "usage": usage, "cached_input_tokens": cached})
                    h.cached_input_tokens += cached
                    chat.set_attribute("gen_ai.output.messages", _output_messages(response))
                    chat.set_attribute("gen_ai.response.model", response.model or "")
                    if usage:
                        chat.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input_tokens") or 0))
                        chat.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output_tokens") or 0))
                if response.status != "completed":
                    raise AgentRunError(f"{display} returned {response.status} output")
                calls = [item for item in response.output if item.type == "function_call"]
                if not calls:
                    text = response.output_text or ""
                    last_text = text
                    if not text.strip():
                        raise AgentRunError(f"{display} returned no text")
                    if not required.issubset(completed):
                        raise AgentRunError(f"{display} finished without the required evidence checks")
                    agent_span.set_attribute("gen_ai.task.output", text)
                    return text
                # Include reasoning items as well as tool calls: stateless turns, no stored conversation.
                history.extend(item.model_dump(mode="json", exclude_none=True) for item in response.output)
                for call in calls:
                    outcome = _dispatch(h, agent, specs, call, once)
                    if outcome.ok:
                        completed.add(call.name)
                    history.append({"type": "function_call_output", "call_id": call.call_id,
                                    "output": json.dumps(outcome.for_model(), sort_keys=True, default=str)})
            raise AgentRunError(f"{display} exceeded its model-turn limit")
        except AgentRunError as exc:
            if agent != "companion" or isinstance(exc, IncompleteAnswer):
                raise
            answer = incomplete_answer(h, str(exc), required, completed, last_text)
            agent_span.set_attribute("gen_ai.task.output", answer)
            raise IncompleteAnswer(str(exc), answer) from exc
