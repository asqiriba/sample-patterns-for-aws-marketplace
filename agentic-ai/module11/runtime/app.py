# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""HTTP entry point of the DevOps Companion runtime.

Serves `GET /ping` and `POST /invocations`.

- `python -m module11.runtime.app --agentcore` (the image CMD) binds 0.0.0.0:8080, which the
  AgentCore Runtime microVM requires, as Module 10's cloud_app.py does. Configuration comes only
  from the `M11_RUNTIME_CONFIG` env value.
- Without `--agentcore` it is a local run: it binds 127.0.0.1:8091 by default and REFUSES any
  non-loopback `--host`. Only a local run accepts `--config-file`, `--fake-model`, `file://`
  data locations, `local:<tool>` handlers and the pass-through guardrail. There is no
  authentication on this port; loopback is the access control.

Bootstrap mode (`bundleArn` and `bundleVersion` null): `/ping` is healthy and every invocation
answers 503 `bootstrapping`. An invalid envelope answers 400 `Invalid invocation`; an invalid
bundle component answers 500 `config_invalid`. Otherwise the response is a server-sent event
stream: `accepted`, one `tool` event per tool call (with `shadow_suppressed` when a side effect
was suppressed), `heartbeat` every 10 s, and a final `result` or `error` carrying the result JSON,
sent after telemetry is flushed so the caller may stop the session at once.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from module10.agent_runner import AgentRun, AgentRunError

from . import envelope as E
from . import guardrail as G
from . import model_retry as MR
from . import results as R
from .config import ENV_NAME, BundleSource, ConfigInvalid, RuntimeConfig, parse_runtime_config, resolve_bundle
from .flags import FlagSource, FlagsUnavailable, flag_enabled
from .loop import BedrockModelClient, HarnessRun, run_harness
from .telemetry import Telemetry, default_backend, parse_baggage
from .tools import CallContext, RecoveryDelegate, RoutingInvoker, ToolClient

MAX_BODY = 128 * 1024
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
LOCAL_DEFAULT = ("127.0.0.1", 8091)
AGENTCORE_BIND = ("0.0.0.0", 8080)  # nosec B104 - required inside the AgentCore microVM only


class HttpError(Exception):
    def __init__(self, status: int, body: dict) -> None:
        super().__init__(body.get("error"))
        self.status = status
        self.body = body


def bind_address(agentcore: bool, host: str | None, port: int | None) -> tuple[str, int]:
    """0.0.0.0:8080 with --agentcore; otherwise loopback only (refuses any other host)."""
    if agentcore:
        return AGENTCORE_BIND
    host = host or LOCAL_DEFAULT[0]
    if host not in LOOPBACK:
        raise SystemExit(f"refusing to bind {host}: a local run binds loopback only (127.0.0.1); "
                         "use --agentcore inside the AgentCore Runtime image")
    return host, port or LOCAL_DEFAULT[1]


def _refuse_local_refs(config: RuntimeConfig) -> None:
    refs = [config["artifactsBucket"], config["snapshot"]["uri"], config["evidence"]["uri"], config["memoryId"],
            config["bundleArn"] or "", *config["toolArns"].values()]
    if any(str(r).startswith(("file:", "local:")) for r in refs) or config["guardrail"]["id"] == G.LOCAL_ID:
        raise ConfigInvalid(f"{ENV_NAME}: local data locations are refused with --agentcore")


class Runtime:
    """One process: config, cached bundle versions and flags, and the invocation flow."""

    def __init__(self, config: RuntimeConfig, *, local: bool, model_client: Any = None, guardrail: Any = None,
                 invoker: Any = None, bundles: BundleSource | None = None, flags: FlagSource | None = None,
                 backend: Any = None) -> None:
        if not local:
            _refuse_local_refs(config)
        self.config = config
        self.local = local
        self.model_client = model_client or BedrockModelClient()
        self.guardrail = guardrail or G.from_config(config["guardrail"], local=local)
        self.invoker = invoker or RoutingInvoker(allow_local=local)
        self.bundles = bundles or BundleSource()
        self.flags = flags or FlagSource(_join(config["artifactsBucket"], config["flagsKey"]))
        self.backend = backend or default_backend()

    # ---------------------------------------------------------- request handling
    def prepare(self, payload: Any, headers: Any) -> tuple[E.Invocation, Any, bool]:
        """Validate before streaming, so bootstrap/400/500 answers are plain HTTP statuses."""
        if self.config.bootstrap:
            raise HttpError(503, {"error": "bootstrapping"})
        try:
            invocation = E.parse(payload)
        except E.EnvelopeError:
            raise HttpError(400, {"error": "Invalid invocation"}) from None
        arn, version, override = resolve_bundle(self.config, parse_baggage(_header(headers, "baggage")))
        try:
            bundle = self.bundles.get(arn, version)
        except ConfigInvalid as exc:
            print(json.dumps({"level": "ERROR", "event": "config_invalid", "detail": str(exc)[:500]}), flush=True)
            raise HttpError(500, {"error": "config_invalid"}) from None
        return invocation, bundle, override

    def investigate(self, invocation: E.Invocation, bundle: Any, override: bool, headers: Any,
                    emit: Callable[[str, dict], None] = lambda name, data: None) -> dict:
        cfg = self.config
        session_id = _header(headers, SESSION_HEADER) or f"local-{uuid.uuid4()}"
        m11 = {**invocation.identity(), "runtimeDigest": cfg["runtimeDigest"], "bundleVersion": bundle.version,
               "bundleOverride": override, "variant": invocation.variant, "conversationId": invocation.conversation_id,
               "runtimeSessionId": session_id, "flagsHash": None, "toolsOffered": [], "cachedInputTokens": 0,
               "guardrail": {"action": "NONE"}}
        run = AgentRun(deadline=time.monotonic() + int(bundle.orchestration["deadlineSeconds"]))
        release, planning, status, review, error, detail = None, False, "failed", None, None, None
        telemetry, h = None, None
        try:
            release, flags_hash, resolved = self.flags.evaluate(invocation.team)
            planning = release["recovery_planning_enabled"] is True
            companion = [t for t in bundle.tools_for("companion") if flag_enabled(resolved, t.requires_flag)]
            m11.update({"flagsHash": flags_hash, "toolsOffered": [t.name for t in companion]})
            common = {"session.id": session_id, "m11.manifest_version": invocation.manifest_version,
                      "m11.behavior_digest": invocation.behavior_digest, "m11.release": invocation.release,
                      "m11.runtime_digest": cfg["runtimeDigest"], "m11.variant": invocation.variant,
                      "m11.conversation_id": invocation.conversation_id, "m11.flags_hash": flags_hash,
                      "m11.flags_resolved": json.dumps(resolved, sort_keys=True), "m11.shadow": invocation.shadow,
                      "m11.stage": cfg["stage"], "m11.bundle_version": bundle.version, "m11.bundle_override": override}
            telemetry = Telemetry(self.backend, common, parent=self.backend.extract(headers, invocation.traceparent))
            ctx = CallContext(stage=cfg["stage"], manifest_version=invocation.manifest_version,
                              behavior_digest=invocation.behavior_digest, release=invocation.release,
                              variant=invocation.variant, session_id=session_id,
                              conversation_id=invocation.conversation_id, request_id=invocation.request_id,
                              team=invocation.team, shadow=invocation.shadow, data=cfg.data_context())
            h = HarnessRun(bundle=bundle, model_client=self.model_client, telemetry=telemetry,
                           tool_client=ToolClient(bundle.tools, dict(cfg["toolArns"]), self.invoker), ctx=ctx,
                           change_summary=invocation.change_summary,
                           advertised={"companion": companion, "planner": bundle.tools_for("planner")},
                           run=run, emit=emit)

            def run_planner(task: str) -> str:
                plan = run_harness(h, "planner", task)
                G.check(self.guardrail, plan, "OUTPUT")
                run.plan_text = plan
                return plan

            h.delegate = RecoveryDelegate(run_planner) if planning else None
            text = run_harness(h, "companion", invocation.change_summary,
                               guard_input=lambda: G.check(self.guardrail, invocation.change_summary, "INPUT"))
            G.check(self.guardrail, text, "OUTPUT")
            status, review = "completed", text
            m11["cachedInputTokens"] = h.cached_input_tokens
        except G.GuardrailBlocked as blocked:
            status, review = "blocked", blocked.message
            m11["guardrail"] = {"action": G.INTERVENED, "source": blocked.source}
        except G.GuardrailUnavailable:
            error, detail = "Guardrail unavailable; the response was withheld.", "guardrail_unavailable"
        except FlagsUnavailable:
            error, detail = "Flag configuration is unavailable.", "flags_unavailable"
        except AgentRunError as exc:
            # The session failed, but the Companion still answers: what it found and what is missing.
            # The answer carries model text, so it passes the OUTPUT guardrail like any review; if the guardrail
            # intervenes or is unavailable, the answer is withheld.
            detail = str(exc)
            answer = getattr(exc, "answer", None)
            if answer:
                try:
                    G.check(self.guardrail, answer, "OUTPUT")
                    review = answer
                except (G.GuardrailBlocked, G.GuardrailUnavailable):
                    review = None
        except Exception as exc:  # never return exception text that could carry request data
            detail = type(exc).__name__
            print(json.dumps({"level": "ERROR", "event": "investigation_failed", "error_type": detail}), flush=True)
        if status != "completed":
            m11["cachedInputTokens"] = sum(int(r.get("cached_input_tokens") or 0) for r in run.responses)
        retries = list(h.model_retries) if h is not None else []
        m11["modelRetries"] = MR.counts(retries)
        MR.emit_metric(cfg["stage"], retries)
        trace_id = telemetry.root_trace_id if telemetry and telemetry.root_trace_id else uuid.uuid4().hex
        body = R.build(invocation=invocation, release=release, planning_enabled=planning, run=run, trace_id=trace_id,
                       m11=m11, status=status, review=review, error=error, detail=detail)
        try:
            R.write(cfg["artifactsBucket"], body)
        except Exception as exc:
            print(json.dumps({"level": "ERROR", "event": "result_write_failed", "error_type": type(exc).__name__}),
                  flush=True)
        if telemetry is not None:
            telemetry.flush()
        return body


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    return value if isinstance(value, str) and value else None


def _join(base: str, key: str) -> str:
    from module11.aws.objects import join
    return join(base, key)


RUNTIME: Runtime | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/ping":
            self._json(200, {"status": "Healthy", "time_of_last_update": int(time.time())})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/invocations":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("body size")
            payload = json.loads(self.rfile.read(length))
        except (ValueError, TypeError):
            self._json(400, {"error": "Invalid invocation"})
            return
        try:
            invocation, bundle, override = RUNTIME.prepare(payload, self.headers)
        except HttpError as exc:
            self._json(exc.status, exc.body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        events: queue.Queue = queue.Queue()

        def write(name: str, data: dict) -> None:
            encoded = f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n".encode()
            self.wfile.write(f"{len(encoded):X}\r\n".encode() + encoded + b"\r\n")
            self.wfile.flush()

        def work() -> None:
            try:
                body = RUNTIME.investigate(invocation, bundle, override, self.headers,
                                           emit=lambda name, data: events.put((name, data)))
                events.put(("result" if body["status"] in {"completed", "blocked"} else "error", body))
            except Exception as exc:  # pragma: no cover - investigate() catches its own failures
                events.put(("error", {"status": "failed", "error": type(exc).__name__,
                                      "request_id": invocation.request_id}))
        try:
            write("accepted", {"request_id": invocation.request_id, "team": invocation.team})
            threading.Thread(target=work, name=f"m11-{invocation.request_id}", daemon=True).start()
            while True:
                try:
                    name, data = events.get(timeout=10)
                except queue.Empty:
                    write("heartbeat", {"request_id": invocation.request_id})
                    continue
                write(name, data)
                if name in {"result", "error"}:
                    break
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *args: Any) -> None:
        pass


def build_runtime(args: argparse.Namespace) -> Runtime:
    if args.agentcore and (args.config_file or args.fake_model):
        raise SystemExit("--config-file and --fake-model are local-run options; --agentcore reads M11_RUNTIME_CONFIG")
    text = Path(args.config_file).read_text(encoding="utf-8") if args.config_file else os.environ.get(ENV_NAME)
    config = parse_runtime_config(text)
    model = None
    if args.fake_model:
        from .fake_model import FakeModelClient
        model = FakeModelClient()
    return Runtime(config, local=not args.agentcore, model_client=model)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m module11.runtime.app", description=__doc__.split("\n\n")[0])
    parser.add_argument("--agentcore", action="store_true", help="bind 0.0.0.0:8080 inside the AgentCore image")
    parser.add_argument("--host", help="local run: loopback host (default 127.0.0.1; others are refused)")
    parser.add_argument("--port", type=int, help="local run: port (default 8091)")
    parser.add_argument("--config-file", help="local run: runtime config JSON instead of M11_RUNTIME_CONFIG")
    parser.add_argument("--fake-model", action="store_true", help="local run: scripted model, no Bedrock calls")
    args = parser.parse_args(argv)
    host, port = bind_address(args.agentcore, args.host, args.port)
    global RUNTIME
    try:
        RUNTIME = build_runtime(args)
    except (ConfigInvalid, OSError, ValueError) as exc:
        print(json.dumps({"level": "ERROR", "event": "config_invalid", "detail": str(exc)[:500]}), flush=True)
        return 1
    mode = "bootstrap mode (invocations answer 503)" if RUNTIME.config.bootstrap else f"stage {RUNTIME.config['stage']}"
    where = "AgentCore Runtime" if args.agentcore else "local run, loopback only, no authentication"
    print(f"m11 runtime listening on {host}:{port} ({where}; {mode})", flush=True)
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
