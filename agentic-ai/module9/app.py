# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
module9/app.py
===============
Plain HTTP server for the Module 9 pipeline agent, mirroring module7/app.py.

Endpoints:
    GET  /ping        health check
    GET  /status      mock flags, corpora, chunk counts
    GET  /lineage     corpus lineage view (?corpus=history)
    GET  /provenance  provenance chain (?doc_id=...)
    POST /ingest      run the ingestion pipeline {"corpus": "history"}
    POST /invoke      ask the agent {"prompt": "...", "session_id": "..."}

Binds to 127.0.0.1 by default and carries no authentication: this is a
local demo server only. Production deployments front the agent with
authenticated infrastructure (the Module 10 exposure patterns and the
AgentCore gateway path documented in the README), never this server.

Binds to 127.0.0.1 by default and has no authentication; local demo use only.
POST bodies must be application/json (at most 1 MiB), and when bound to
loopback the Host header must be a loopback name.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# Ensure the project root (agentic-ai/) is on the path when running directly
# as `python module9/app.py`. Module 9 imports its packages lazily.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = int(os.getenv("MODULE9_PORT", "8089"))
HOST = os.getenv("MODULE9_HOST", "127.0.0.1")
MAX_BODY_BYTES = 1024 * 1024
_LOOPBACK_HOST_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _is_loopback(host: str) -> bool:
    """True when a bind address only accepts connections from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

_AGENT = None
_SESSION_ID = None


def _get_agent():
    """Lazy singleton agent, created on first /invoke."""
    global _AGENT, _SESSION_ID
    if _AGENT is None:
        from module9.agent import create_pipeline_agent

        _AGENT, _SESSION_ID = create_pipeline_agent(verbose=False)
    return _AGENT, _SESSION_ID


class Module9Handler(BaseHTTPRequestHandler):
    """HTTP handler for pipeline operations and agent invocation."""

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_internal_error(self, exc: Exception) -> None:
        """Generic 500: the exception detail goes to stderr, never to the client."""
        print(f"[Module9] {type(exc).__name__}: {exc}", file=sys.stderr)
        self._send_json({"error": "internal server error", "detail": type(exc).__name__}, 500)

    def _host_allowed(self) -> bool:
        """Refuse non-loopback Host headers when bound to loopback (DNS rebinding)."""
        if not _is_loopback(str(self.server.server_address[0])):
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            hostname = host.split("]", 1)[0] + "]"
        else:
            hostname = host.split(":", 1)[0]
        if hostname in _LOOPBACK_HOST_NAMES:
            return True
        self._send_json({"error": "forbidden host"}, 403)
        return False

    def _read_json_body(self) -> dict | None:
        """Return the JSON object body ({} when empty), or send a 4xx and return None."""
        raw_length = (self.headers.get("Content-Length") or "").strip()
        if not (raw_length.isascii() and raw_length.isdigit()):
            self._send_json({"error": "a valid Content-Length header is required"}, 400)
            return None
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self._send_json({"error": f"request body exceeds {MAX_BODY_BYTES} bytes"}, 413)
            return None
        # Read the declared body before any further check so the client
        # always receives the error response instead of a connection reset.
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._send_json({"error": "Content-Type must be application/json"}, 415)
            return None
        try:
            data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "Invalid JSON body"}, 400)
            return None
        if not isinstance(data, dict):
            self._send_json({"error": "request body must be a JSON object"}, 400)
            return None
        return data

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if not self._host_allowed():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/ping":
            self._send_json({"status": "ok", "module": 9})
            return

        if parsed.path == "/status":
            from module9.config.corpora import CORPORA
            from module9.ingestion.lineage import get_registry

            registry = get_registry()
            self._send_json(
                {
                    "module": 9,
                    "mock_pipeline": os.getenv("AGENT_MOCK_PIPELINE", "").lower()
                    == "true",
                    "mock_memory": os.getenv("AGENT_MOCK_MEMORY", "").lower()
                    == "true",
                    "corpora": {
                        name: len(registry.chunks_for_corpus(name))
                        for name in CORPORA
                    },
                }
            )
            return

        if parsed.path == "/lineage":
            from module9.ingestion.lineage import get_lineage

            corpus = (query.get("corpus") or ["history"])[0]
            self._send_json(get_lineage(corpus))
            return

        if parsed.path == "/provenance":
            from module9.ingestion.lineage import get_provenance

            doc_id = (query.get("doc_id") or [""])[0]
            if not doc_id:
                self._send_json({"error": "doc_id query parameter required"}, 400)
                return
            self._send_json(get_provenance(doc_id))
            return

        self._send_json({"error": f"Unknown path {parsed.path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if not self._host_allowed():
            return
        data = self._read_json_body()
        if data is None:
            return

        if self.path == "/ingest":
            from module9.identity import Auth0Error
            from module9.ingestion.pipeline_run import run_pipeline

            try:
                result = run_pipeline(
                    corpus=str(data.get("corpus", "history")),
                    max_events=int(data.get("max_events", 10)),
                )
            except Auth0Error as exc:
                self._send_json({"error": f"Authorization denied: {exc}"}, 403)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            except Exception as exc:
                self._send_internal_error(exc)
                return
            self._send_json(result.summary())
            return

        if self.path == "/invoke":
            prompt = data.get("prompt")
            if not prompt:
                self._send_json({"error": "prompt field required"}, 400)
                return
            try:
                agent, default_sid = _get_agent()
                sid = data.get("session_id") or default_sid
                result = agent.invoke(
                    {"messages": [("user", str(prompt))]},
                    config={"configurable": {"thread_id": sid}},
                )
                messages = result.get("messages", [])
                answer = messages[-1].content if messages else ""
                self._send_json({"response": answer, "session_id": sid})
            except Exception as exc:
                self._send_internal_error(exc)
            return

        self._send_json({"error": f"Unknown path {self.path}"}, 404)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # keep demo output clean


def run_server(port: int = PORT, host: str = HOST) -> None:
    server = HTTPServer((host, port), Module9Handler)
    if not _is_loopback(host):
        print("WARNING: no authentication; reachable from your network")
    print(f"Module 9 pipeline agent listening on http://{host}:{port}")
    print("Endpoints: GET /ping /status /lineage /provenance; POST /ingest /invoke")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    run_server()
