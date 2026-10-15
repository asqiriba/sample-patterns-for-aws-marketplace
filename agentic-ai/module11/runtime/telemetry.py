# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Spans that AgentCore Evaluations can read (this shape was verified live).

Scope `opentelemetry.instrumentation.devops_companion` with GenAI operations `invoke_agent`,
`execute_tool` and `chat`. Every span also carries the Module 11 attributes: the release
identity from the envelope, `m11.runtime_digest` from the env, the bundle version actually used
and whether baggage overrode it, plus variant, conversation, flags, shadow, stage and
`session.id` (set explicitly).

The image runs under `opentelemetry-instrument` with ADOT and `OTEL_TRACES_SAMPLER=always_on`
(deployment/Dockerfile). Without OpenTelemetry installed (tooling venv, M13) the no-op backend
is used; tests use `RecordingBackend`. Inbound headers are read through a case-insensitive
getter, because lower-casing them broke the X-Ray propagator in a live test.
"""
from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import unquote

SCOPE = "opentelemetry.instrumentation.devops_companion"


def _value(value: Any) -> Any:
    if isinstance(value, (str, bool, int, float)):
        return value
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, default=str)


def parse_baggage(header: str | None) -> dict[str, str]:
    """W3C baggage entries. Uses OpenTelemetry's propagator when installed, otherwise the
    same list-member grammar: `key=value;properties`, comma-separated, percent-encoded values.
    Properties are metadata, not part of the value; OpenTelemetry's Python propagator keeps them
    in the value, so they are dropped here in both paths."""
    if not header:
        return {}
    try:
        from opentelemetry import baggage
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        context = W3CBaggagePropagator().extract({"baggage": header})
        return {k: str(v).split(";", 1)[0].strip() for k, v in baggage.get_all(context).items()}
    except ImportError:
        pass
    out = {}
    for member in header.split(","):
        pair = member.split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key.strip():
            out[unquote(key.strip())] = unquote(value.strip())
    return out


class _HeaderGetter:
    """TextMap getter over `http.server` headers (email.message.Message: case-insensitive)."""

    def get(self, carrier: Any, key: str):
        value = carrier.get(key) if hasattr(carrier, "get") else None
        return [value] if value is not None else None

    def keys(self, carrier: Any) -> list[str]:
        return list(carrier.keys()) if hasattr(carrier, "keys") else []


# ---------------------------------------------------------------- backends
class _NoopSpan:
    def __init__(self) -> None:
        self.trace_id = uuid.uuid4().hex

    def set_attribute(self, key: str, value: Any) -> None:
        pass


class NoopBackend:
    def extract(self, headers: Any, traceparent: str | None) -> Any:
        return None

    @contextmanager
    def span(self, name: str, attributes: dict, parent: Any = None) -> Iterator[_NoopSpan]:
        yield _NoopSpan()

    def trace_id(self, span: Any) -> str:
        return span.trace_id

    def flush(self) -> bool:
        return True


@dataclass
class RecordedSpan:
    name: str
    attributes: dict
    parent: "RecordedSpan | None"
    trace_id: str
    start: int
    end: int | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = _value(value)


@dataclass
class RecordingBackend:
    """In-memory spans for tests: start/end order, parents, attributes."""
    spans: list[RecordedSpan] = field(default_factory=list)
    flushes: int = 0
    _clock: int = 0
    _local: threading.local = field(default_factory=threading.local)

    def extract(self, headers: Any, traceparent: str | None) -> Any:
        return traceparent

    @contextmanager
    def span(self, name: str, attributes: dict, parent: Any = None) -> Iterator[RecordedSpan]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        up = stack[-1] if stack else None
        trace = up.trace_id if up else (parent.split("-")[1] if isinstance(parent, str) else uuid.uuid4().hex)
        self._clock += 1
        span = RecordedSpan(name, {k: _value(v) for k, v in attributes.items()}, up, trace, self._clock)
        self.spans.append(span)
        stack.append(span)
        try:
            yield span
        finally:
            stack.pop()
            self._clock += 1
            span.end = self._clock

    def trace_id(self, span: RecordedSpan) -> str:
        return span.trace_id

    def flush(self) -> bool:
        self.flushes += 1
        return True


class OtelBackend:
    """The OpenTelemetry API; ADOT configures the provider and exporters in the image."""

    def __init__(self) -> None:
        from opentelemetry import trace
        self._trace = trace
        self._tracer = trace.get_tracer(SCOPE)

    def extract(self, headers: Any, traceparent: str | None) -> Any:
        from opentelemetry import propagate
        context = propagate.extract(headers, getter=_HeaderGetter()) if headers is not None else None
        valid = context is not None and self._trace.get_current_span(context).get_span_context().is_valid
        if not valid and traceparent:
            context = propagate.extract({"traceparent": traceparent})
        return context

    @contextmanager
    def span(self, name: str, attributes: dict, parent: Any = None):
        with self._tracer.start_as_current_span(name, context=parent,
                                                attributes={k: _value(v) for k, v in attributes.items()}) as span:
            yield span

    def trace_id(self, span: Any) -> str:
        return format(span.get_span_context().trace_id, "032x")

    def flush(self) -> bool:
        provider = self._trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        ok = bool(flush()) if flush else True
        try:  # ADOT also installs a logger provider; flush it before the final event
            from opentelemetry import _logs
            log_flush = getattr(_logs.get_logger_provider(), "force_flush", None)
            if log_flush:
                log_flush()
        except ImportError:
            pass
        return ok


def default_backend() -> Any:
    try:
        return OtelBackend()
    except ImportError:
        return NoopBackend()


# ---------------------------------------------------------------- per-invocation session
class Telemetry:
    """Spans for one invocation. `common` holds the Module 11 attributes put on every span."""

    def __init__(self, backend: Any, common: dict[str, Any], parent: Any = None) -> None:
        self.backend = backend
        self.common = dict(common)
        self.parent = parent
        self.root_trace_id: str | None = None

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        attrs = {**self.common, **(attributes or {})}
        parent = self.parent if self.root_trace_id is None else None
        with self.backend.span(name, attrs, parent) as span:
            if self.root_trace_id is None:
                self.root_trace_id = self.backend.trace_id(span)
            yield span

    def invoke_agent(self, agent: str, task: str):
        return self.span(f"invoke_agent {agent}", {"gen_ai.operation.name": "invoke_agent",
                                                   "gen_ai.agent.name": agent, "gen_ai.task.input": task})

    def execute_tool(self, tool: str, arguments: Any, call_id: str):
        return self.span(f"execute_tool {tool}", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool,
                                                  "gen_ai.tool.call.id": call_id,
                                                  "gen_ai.tool.call.arguments": arguments})

    def chat(self, model: str, instructions: str, messages: list[dict]):
        return self.span("chat", {"gen_ai.operation.name": "chat", "gen_ai.request.model": model,
                                  "gen_ai.system_instructions": [{"type": "text", "content": instructions}],
                                  "gen_ai.input.messages": messages})

    def flush(self) -> bool:
        return self.backend.flush()
