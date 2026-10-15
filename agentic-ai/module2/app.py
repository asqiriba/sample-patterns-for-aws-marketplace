# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
module2/app.py
==============
Standalone HTTP server for Module 2 Repository Analysis Agent.

This provides a simple HTTP interface for repository analysis without
requiring AgentCore deployment. Useful for local development and testing.

ENDPOINTS
---------
POST /analyze  - Analyze a repository
GET  /ping     - Health check

USAGE
-----
  # Start the server
  python module2/app.py

  # Or with mock mode
  AGENT_MOCK_REPO=true python module2/app.py

  # Analyze a repository
  curl -X POST http://localhost:8081/analyze \\
    -H "Content-Type: application/json" \\
    -d '{"repo_path": "/path/to/repo"}'

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

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from module2.agent import create_agent


# ---------------------------------------------------------------------------
# Shared agent instance
# ---------------------------------------------------------------------------

print("\n  Initializing Module 2 Repository Analysis Agent...")
_agent = create_agent(verbose=False)
print("  Agent ready.\n")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _handle_analysis(payload: dict) -> dict:
    """
    Process a repository analysis request.

    Expected payload
    ----------------
    {
      "repo_path": str   — required: absolute path to git repository
      "verbose": bool    — optional: print agent steps (default: false)
    }

    Returns
    -------
    dict
        Analysis results with applications, stacks, and AWS requirements.
    """
    repo_path = payload.get("repo_path", "").strip()
    if not repo_path:
        return {"error": "Missing required field: 'repo_path'"}

    # The agent is a LangGraph ReAct graph: it takes a "messages" list and
    # returns the conversation, whose last message is the analysis.
    agent_input = {"messages": [("user", f"Analyze the git repository at: {repo_path}")]}

    # Use verbose agent for local testing if requested
    if payload.get("verbose"):
        local_agent = create_agent(verbose=True)
        result = local_agent.invoke(agent_input)
    else:
        result = _agent.invoke(agent_input)

    messages = result.get("messages", [])
    return {
        "repo_path": repo_path,
        "analysis": messages[-1].content if messages else "",
        "mock_mode": os.getenv("AGENT_MOCK_REPO", "false").lower() == "true",
        "framework": "langchain",
    }


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

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


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler for repository analysis API."""

    def log_message(self, *_: object) -> None:
        """Suppress default access log."""
        pass

    def do_GET(self) -> None:
        """Handle GET requests."""
        if not self._host_allowed():
            return
        if self.path == "/ping":
            self._respond(200, {"status": "ok", "service": "module2-repo-analysis"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Handle POST requests."""
        if not self._host_allowed():
            return
        if self.path != "/analyze":
            self._respond(404, {"error": "not found"})
            return

        body = self._read_json_body()
        if body is None:
            return

        try:
            result = _handle_analysis(body)
        except Exception as exc:  # generic 500: never echo exception text
            print(f"[Module2] {type(exc).__name__}: {exc}", file=sys.stderr)
            self._respond(500, {"error": "internal server error", "detail": type(exc).__name__})
            return
        self._respond(200, result)

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
        self._respond(403, {"error": "forbidden host"})
        return False

    def _read_json_body(self) -> dict | None:
        """Return the JSON object body, or send a 4xx and return None."""
        raw_length = (self.headers.get("Content-Length") or "").strip()
        if not (raw_length.isascii() and raw_length.isdigit()):
            self._respond(400, {"error": "a valid Content-Length header is required"})
            return None
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self._respond(413, {"error": f"request body exceeds {MAX_BODY_BYTES} bytes"})
            return None
        # Read the declared body before any further check so the client
        # always receives the error response instead of a connection reset.
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._respond(415, {"error": "Content-Type must be application/json"})
            return None
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, {"error": "invalid JSON"})
            return None
        if not isinstance(body, dict):
            self._respond(400, {"error": "request body must be a JSON object"})
            return None
        return body

    def _respond(self, code: int, data: dict) -> None:
        """Send JSON response."""
        payload = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run_server(host: str = "127.0.0.1", port: int = 8081) -> None:
    """
    Run the HTTP server.

    Parameters
    ----------
    host : str
        Host to bind to. Default: 127.0.0.1 (localhost only).
        Set to 0.0.0.0 explicitly to bind to all interfaces.
    port : int
        Port to listen on. Default: 8081 (different from Module 1's 8080)
    """
    mock = os.getenv("AGENT_MOCK_REPO", "false").lower() == "true"
    if not _is_loopback(host):
        print("  WARNING: no authentication; reachable from your network")
    
    print(f"  🚀  Module 2 Repository Analysis Agent HTTP Server")
    print(f"      http://{host}:{port}/analyze  (POST)")
    print(f"      http://{host}:{port}/ping     (GET)")
    print(f"      Mock mode : {'ON — using fixture data' if mock else 'OFF — analyzing real repos'}")
    print(f"\n  Example:")
    print(f"    curl -X POST http://localhost:{port}/analyze \\")
    print(f"      -H 'Content-Type: application/json' \\")
    print(f"      -d '{{\"repo_path\": \"/path/to/repo\"}}'")
    print(f"\n  Ctrl-C to stop.\n")

    server = HTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_server()
