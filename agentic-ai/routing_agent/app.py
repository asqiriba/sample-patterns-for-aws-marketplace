# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
routing-agent/app.py
====================
HTTP server for routing agent.

Provides REST API endpoint for intent classification and routing.

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
# as `python routing_agent/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routing_agent.agent import classify_intent, route_request


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("ROUTING_PORT", "8083"))
HOST = os.getenv("ROUTING_HOST", "127.0.0.1")
VERBOSE = os.getenv("ROUTING_VERBOSE", "false").lower() == "true"
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
# HTTP Request Handler
# ---------------------------------------------------------------------------

class RoutingHandler(BaseHTTPRequestHandler):
    """HTTP request handler for routing agent endpoints."""

    def _send_json_response(self, data: dict[str, Any], status: int = 200) -> None:
        """Send JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _send_error_response(self, message: str, status: int = 400) -> None:
        """Send error response."""
        self._send_json_response({"error": message}, status)

    def _send_internal_error(self, exc: Exception) -> None:
        """Generic 500: the exception detail goes to stderr, never to the client."""
        print(f"[RoutingAgent] {type(exc).__name__}: {exc}", file=sys.stderr)
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
        """Handle GET requests."""
        if not self._host_allowed():
            return
        if self.path == "/ping":
            self._send_json_response({
                "status": "healthy",
                "service": "routing-agent",
                "version": "0.1.0",
            })
        elif self.path == "/categories":
            # Return available categories
            from routing_agent.prompts.routing_prompts import CATEGORY_DESCRIPTIONS
            self._send_json_response({
                "categories": CATEGORY_DESCRIPTIONS,
            })
        else:
            self._send_error_response("Not found", 404)

    def do_POST(self) -> None:
        """Handle POST requests."""
        try:
            if not self._host_allowed():
                return
            # Parse request body
            data = self._read_json_body()
            if data is None:
                return

            # Route to appropriate handler
            if self.path == "/route":
                self._handle_route(data)
            elif self.path == "/classify":
                self._handle_classify(data)
            else:
                self._send_error_response("Not found", 404)

        except Exception as e:
            self._send_internal_error(e)

    def _handle_route(self, data: dict[str, Any]) -> None:
        """
        Handle /route endpoint - full routing with suggested action.
        
        Request body:
        {
            "request": "User's request text"
        }
        """
        request_text = data.get("request")
        if not request_text:
            self._send_error_response("Missing 'request' in request body")
            return

        try:
            result = route_request(
                request=request_text,
                verbose=VERBOSE,
            )
            
            self._send_json_response({
                "status": "success",
                "routing": result,
            })
        except Exception as e:
            self._send_internal_error(e)

    def _handle_classify(self, data: dict[str, Any]) -> None:
        """
        Handle /classify endpoint - classification only (no routing metadata).
        
        Request body:
        {
            "request": "User's request text"
        }
        """
        request_text = data.get("request")
        if not request_text:
            self._send_error_response("Missing 'request' in request body")
            return

        try:
            result = classify_intent(
                request=request_text,
                verbose=VERBOSE,
            )
            
            self._send_json_response({
                "status": "success",
                "classification": result,
            })
        except Exception as e:
            self._send_internal_error(e)

    def log_message(self, format: str, *args: Any) -> None:
        """Override to customize logging."""
        if VERBOSE:
            print(f"[RoutingAgent] {format % args}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def run_server(port: int = PORT, host: str = HOST) -> None:
    """
    Run the routing agent HTTP server.

    Parameters
    ----------
    port : int
        Port to listen on. Default 8083.
    host : str
        Host to bind to. Default "127.0.0.1" (localhost only).
        Set ROUTING_HOST=0.0.0.0 to bind to all interfaces.
    """
    server = HTTPServer((host, port), RoutingHandler)
    if not _is_loopback(host):
        print("  WARNING: no authentication; reachable from your network")
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  Routing Agent: Intent Classification & Request Routing         ║
╚══════════════════════════════════════════════════════════════════╝

  Server running on http://{host}:{port}

  Endpoints:
    GET  /ping              - Health check
    GET  /categories        - List available categories
    POST /classify          - Classify intent only
    POST /route             - Classify and provide routing info

  Categories:
    • repository_analysis    → Module 2 (port 8081)
    • infrastructure_generation → Module 3 (port 8082)
    • aws_infrastructure     → Module 1 (port 8080)
    • deployment_monitoring  → Future capability

  Example:
    curl -X POST http://localhost:{port}/route \\
      -H "Content-Type: application/json" \\
      -d '{{"request": "Analyze my repository at /home/user/app"}}'

  Press Ctrl+C to stop
""")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nShutting down server...")
        server.shutdown()


if __name__ == "__main__":
    run_server()
