# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
module5/app.py
==============
HTTP server for Module 5 Domain-Specific Agent Applications.

Provides REST API endpoints for domain-specialized agent invocations.
Runs on port 8085 by default.

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
# as `python module5/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from module5.agent import create_domain_agent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("MODULE5_PORT", "8085"))
HOST = os.getenv("MODULE5_HOST", "127.0.0.1")
VERBOSE = os.getenv("MODULE5_VERBOSE", "false").lower() == "true"
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
# Agent cache — initialized once at startup, reused across requests
# ---------------------------------------------------------------------------

_AGENTS: dict = {}


def _get_agent(domain: str):
    """Return a cached domain agent, creating it on first use."""
    if domain not in _AGENTS:
        _AGENTS[domain] = create_domain_agent(domain, verbose=VERBOSE)
    return _AGENTS[domain]

# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class Module5Handler(BaseHTTPRequestHandler):
    """HTTP request handler for Module 5 domain agent endpoints."""

    def _send_json_response(self, data: dict[str, Any], status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _send_error_response(self, message: str, status: int = 400) -> None:
        self._send_json_response({"error": message}, status)

    def _send_internal_error(self, exc: Exception) -> None:
        """Generic 500: the exception detail goes to stderr, never to the client."""
        print(f"[Module5] {type(exc).__name__}: {exc}", file=sys.stderr)
        self._send_json_response(
            {"error": "internal server error", "detail": type(exc).__name__}, 500
        )

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
        self._send_error_response("forbidden host", 403)
        return False

    def _read_json_body(self) -> dict[str, Any] | None:
        """Return the JSON object body ({} when empty), or send a 4xx and return None."""
        raw_length = (self.headers.get("Content-Length") or "").strip()
        if not (raw_length.isascii() and raw_length.isdigit()):
            self._send_error_response("a valid Content-Length header is required", 400)
            return None
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self._send_error_response(f"request body exceeds {MAX_BODY_BYTES} bytes", 413)
            return None
        # Read the declared body before any further check so the client
        # always receives the error response instead of a connection reset.
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._send_error_response("Content-Type must be application/json", 415)
            return None
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error_response("Invalid JSON in request body")
            return None
        if not isinstance(data, dict):
            self._send_error_response("request body must be a JSON object")
            return None
        return data

    def do_GET(self) -> None:
        if not self._host_allowed():
            return
        if self.path == "/ping":
            self._send_json_response({
                "status": "healthy",
                "service": "module5-domain-agents",
                "version": "0.1.0",
                "domains": ["customer", "geospatial", "voice"],
            })
        else:
            self._send_error_response("Not found", 404)

    def do_POST(self) -> None:
        try:
            if not self._host_allowed():
                return
            data = self._read_json_body()
            if data is None:
                return

            if self.path == "/invoke":
                self._handle_invoke(data)
            else:
                self._send_error_response("Not found", 404)

        except Exception as e:
            self._send_internal_error(e)

    def _handle_invoke(self, data: dict[str, Any]) -> None:
        """
        Handle /invoke endpoint.

        Request body:
        {
            "domain": "customer",
            "message": "Find me a monitoring tool"
        }
        """
        domain = data.get("domain")
        message = data.get("message")

        if not domain:
            self._send_error_response("Missing 'domain' in request body")
            return
        if not message:
            self._send_error_response("Missing 'message' in request body")
            return

        try:
            agent = _get_agent(domain)
            result = agent.agent.invoke({"messages": [("user", message)]})
            messages = result.get("messages", [])
            output = messages[-1].content if messages else ""
            self._send_json_response({"status": "success", "domain": domain, "output": output})
        except ValueError as e:
            self._send_error_response(str(e), 400)
        except Exception as e:
            self._send_internal_error(e)

    def log_message(self, format: str, *args: Any) -> None:
        if VERBOSE:
            print(f"[Module5] {format % args}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def run_server(port: int = PORT, host: str = HOST) -> None:
    server = HTTPServer((host, port), Module5Handler)
    if not _is_loopback(host):
        print("  WARNING: no authentication; reachable from your network")
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  Module 5: Domain-Specific Agent Applications                   ║
╚══════════════════════════════════════════════════════════════════╝

  Server running on http://{host}:{port}

  Endpoints:
    GET  /ping    - Health check
    POST /invoke  - Invoke a domain agent

  Domains: customer | geospatial | voice

  Example:
    curl -X POST http://localhost:{port}/invoke \\
      -H "Content-Type: application/json" \\
      -d '{{"domain": "customer", "message": "Find me a monitoring tool"}}'

  Press Ctrl+C to stop
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nShutting down server...")
        server.shutdown()


if __name__ == "__main__":
    run_server()
