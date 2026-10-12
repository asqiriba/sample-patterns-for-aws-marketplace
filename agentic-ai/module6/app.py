# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
module6/app.py
===============
HTTP server for Module 6 on port 8086.

Provides endpoints to start workflows, send signals, and query status.
In production, this would be a thin API gateway that delegates to
Temporal Cloud. EventBridge rules can target this via Lambda.

Binds to 127.0.0.1 by default and has no authentication; local demo use only.
POST bodies must be application/json (at most 1 MiB), and when bound to
loopback the Host header must be a loopback name.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

# Ensure the project root (agentic-ai/) is on the path when running directly
# as `python module6/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from module6.workflows.devops_companion import DevOpsCompanionWorkflow


MAX_BODY_BYTES = 1024 * 1024
_LOOPBACK_HOST_NAMES = {"127.0.0.1", "localhost", "[::1]"}

_workflow: DevOpsCompanionWorkflow | None = None


def _is_loopback(host: str) -> bool:
    """True when a bind address only accepts connections from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _get_workflow() -> DevOpsCompanionWorkflow:
    global _workflow
    if _workflow is None:
        _workflow = DevOpsCompanionWorkflow()
    return _workflow


class Module6Handler(BaseHTTPRequestHandler):
    """HTTP request handler for Module 6 API."""

    def do_GET(self) -> None:
        if not self._host_allowed():
            return
        try:
            if self.path == "/ping":
                self._respond(200, {"status": "ok", "module": 6, "framework": "temporal"})
            elif self.path == "/status":
                wf = _get_workflow()
                self._respond(200, wf.get_status())
            elif self.path == "/workers":
                wf = _get_workflow()
                self._respond(200, wf.get_worker_info())
            else:
                self._respond(404, {"error": "Not found"})
        except Exception as exc:
            self._internal_error(exc)

    def do_POST(self) -> None:
        if not self._host_allowed():
            return
        body = self._read_json_body()
        if body is None:
            return

        try:
            if self.path == "/start":
                wf = _get_workflow()
                repo = body.get("repo_path", "/app/nodejs-api")
                region = body.get("region", "us-east-1")
                result = wf.start(repo, region)
                self._respond(200, result)

            elif self.path == "/signal":
                wf = _get_workflow()
                approved = body.get("approved", True)
                reviewer = body.get("reviewer", "api-user")
                comments = body.get("comments", "")
                result = wf.submit_approval(approved, reviewer, comments)
                self._respond(200, result)

            elif self.path == "/traces":
                wf = _get_workflow()
                result = wf.get_traces()
                self._respond(200, result)

            else:
                self._respond(404, {"error": "Not found"})
        except Exception as exc:
            self._internal_error(exc)

    def _internal_error(self, exc: Exception) -> None:
        """Generic 500: the exception detail goes to stderr, never to the client."""
        print(f"[Module6] {type(exc).__name__}: {exc}", file=sys.stderr)
        self._respond(500, {"error": "internal server error", "detail": type(exc).__name__})

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

    def _read_json_body(self) -> dict[str, Any] | None:
        """Return the JSON object body ({} when empty), or send a 4xx and return None."""
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
        if length == 0:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._respond(400, {"error": "invalid JSON"})
            return None
        if not isinstance(data, dict):
            self._respond(400, {"error": "request body must be a JSON object"})
            return None
        return data

    def _respond(self, status: int, data: dict[str, Any]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    port = int(os.getenv("MODULE6_PORT", "8086"))
    # Bind to loopback by default. Set MODULE6_HOST=0.0.0.0 to bind to all
    # interfaces (e.g. when running inside a container).
    host = os.getenv("MODULE6_HOST", "127.0.0.1")
    server = HTTPServer((host, port), Module6Handler)
    if not _is_loopback(host):
        print("[Module 6] WARNING: no authentication; reachable from your network")
    print(f"[Module 6] Server running on {host}:{port}")
    print(f"[Module 6] Endpoints: /ping, /status, /workers, /start, /signal, /traces")
    server.serve_forever()


if __name__ == "__main__":
    main()
