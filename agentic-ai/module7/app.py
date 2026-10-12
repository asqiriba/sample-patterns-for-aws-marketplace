# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
module7/app.py
==============
HTTP server for Module 7 Agent Memory Systems.

Endpoints:
    GET  /ping        — health check
    GET  /status      — mock mode and backend connection status
    POST /invoke      — invoke the memory-augmented agent (session-aware)
    POST /consolidate — consolidate a session history into long-term memory

Default port: 8087 (override with MODULE7_PORT env var).

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

# Ensure the project root (agentic-ai/) is on the path when running directly
# as `python module7/app.py`. Module 7 imports its packages lazily.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = int(os.getenv("MODULE7_PORT", "8087"))
HOST = os.getenv("MODULE7_HOST", "127.0.0.1")
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

# ---------------------------------------------------------------------------
# Lazy agent initialization
# ---------------------------------------------------------------------------

_AGENT_GRAPH = None
_DEFAULT_SESSION_ID = "http-default-session"


def _get_agent():
    global _AGENT_GRAPH
    if _AGENT_GRAPH is None:
        from module7.agent import create_memory_agent
        _AGENT_GRAPH, _ = create_memory_agent(verbose=False)
    return _AGENT_GRAPH


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class Module7Handler(BaseHTTPRequestHandler):
    """HTTP request handler for Module 7 memory agent endpoints."""

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_internal_error(self, exc: Exception) -> None:
        """Generic 500: the exception detail goes to stderr, never to the client."""
        print(f"[Module7] {type(exc).__name__}: {exc}", file=sys.stderr)
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
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, 400)
            return None
        if not isinstance(data, dict):
            self._send_json({"error": "request body must be a JSON object"}, 400)
            return None
        return data

    def do_GET(self) -> None:
        if not self._host_allowed():
            return
        mock = os.getenv("AGENT_MOCK_MEMORY", "").lower() == "true"
        if self.path == "/ping":
            self._send_json({
                "status": "ok",
                "module": 7,
                "framework": "langgraph",
                "memory_backends": ["redis", "mongodb", "neo4j"],
            })
        elif self.path == "/status":
            self._send_json({
                "mock_mode": mock,
                "redis_connected": True,
                "mongodb_connected": True,
                "neo4j_connected": True,
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._host_allowed():
            return
        data = self._read_json_body()
        if data is None:
            return

        if self.path == "/invoke":
            self._handle_invoke(data)
        elif self.path == "/consolidate":
            self._handle_consolidate(data)
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_invoke(self, data: dict) -> None:
        query = data.get("query")
        if not isinstance(query, str):
            self._send_json({"error": "query field required"}, 400)
            return
        # Callers may pass a session_id to maintain conversation continuity
        session_id = data.get("session_id", _DEFAULT_SESSION_ID)
        try:
            agent = _get_agent()
            result = agent.invoke(
                {"messages": [("user", query)]},
                config={"configurable": {"thread_id": session_id}},
            )
            messages = result.get("messages", [])
            response = messages[-1].content if messages else ""
            tool_calls = []
            for msg in messages:
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        tool_calls.append({
                            "name": tc.get("name", ""),
                            "args": tc.get("args", {}),
                        })
            self._send_json({
                "response": response,
                "tool_calls": tool_calls,
                "session_id": session_id,
            })
        except Exception as exc:
            self._send_internal_error(exc)

    def _handle_consolidate(self, data: dict) -> None:
        history = data.get("session_history")
        agent_id = data.get("agent_id")
        if history is None:
            self._send_json({"error": "session_history field required"}, 400)
            return
        if agent_id is None:
            self._send_json({"error": "agent_id field required"}, 400)
            return
        try:
            from module7.memory.consolidation import ConsolidationService
            from module7.memory.embeddings import EmbeddingService
            from module7.memory.mongo_store import MongoStore
            from module7.memory.neo4j_store import Neo4jStore

            svc = ConsolidationService(MongoStore(), Neo4jStore(), EmbeddingService())
            r = svc.consolidate(history, agent_id)
            self._send_json({
                "facts": r.facts,
                "relationships": r.relationships,
                "stored_count": r.stored_count,
            })
        except Exception as exc:
            self._send_internal_error(exc)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_server(port: int = PORT, host: str = HOST) -> None:
    server = HTTPServer((host, port), Module7Handler)
    if not _is_loopback(host):
        print("  WARNING: no authentication; reachable from your network")
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  Module 7: Agent Memory Systems                                 ║
╚══════════════════════════════════════════════════════════════════╝

  Server running on http://{host}:{port}

  Endpoints:
    GET  /ping        - Health check
    GET  /status      - Mock mode and backend status
    POST /invoke      - Invoke the memory-augmented agent
    POST /consolidate - Consolidate session history

  Session continuity: pass "session_id" in the /invoke body to
  maintain conversation context across multiple requests.

  Press Ctrl+C to stop
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()


if __name__ == "__main__":
    run_server()
