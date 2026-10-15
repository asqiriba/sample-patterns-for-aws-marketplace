# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
module3/app.py
==============
HTTP server for Module 3 CDK Infrastructure Generation Agent.

Provides REST API endpoints for CDK generation and validation.

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
# as `python module3/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from module3.agent import create_agent, generate_infrastructure, validate_cdk_code


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("MODULE3_PORT", "8082"))
HOST = os.getenv("MODULE3_HOST", "127.0.0.1")
VERBOSE = os.getenv("MODULE3_VERBOSE", "false").lower() == "true"
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

class Module3Handler(BaseHTTPRequestHandler):
    """HTTP request handler for Module 3 CDK generation endpoints."""

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
        print(f"[Module3] {type(exc).__name__}: {exc}", file=sys.stderr)
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
                "service": "module3-cdk-agent",
                "version": "0.1.0",
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
            if self.path == "/generate":
                self._handle_generate(data)
            elif self.path == "/validate":
                self._handle_validate(data)
            elif self.path == "/analyze":
                self._handle_analyze(data)
            else:
                self._send_error_response("Not found", 404)

        except Exception as e:
            self._send_internal_error(e)

    def _handle_generate(self, data: dict[str, Any]) -> None:
        """
        Handle /generate endpoint.
        
        Request body:
        {
            "requirements": {...} or "text requirements",
            "region": "us-east-1",
            "environment": "dev"
        }
        """
        requirements = data.get("requirements")
        if not requirements:
            self._send_error_response("Missing 'requirements' in request body")
            return

        region = data.get("region", "us-east-1")
        environment = data.get("environment", "dev")

        try:
            result = generate_infrastructure(
                requirements=requirements,
                region=region,
                environment=environment,
                verbose=VERBOSE,
            )
            
            self._send_json_response({
                "status": "success",
                "region": region,
                "environment": environment,
                "output": result["output"],
            })
        except Exception as e:
            self._send_internal_error(e)

    def _handle_validate(self, data: dict[str, Any]) -> None:
        """
        Handle /validate endpoint.
        
        Request body:
        {
            "cdk_code": "from aws_cdk import ..."
        }
        """
        cdk_code = data.get("cdk_code")
        if not cdk_code:
            self._send_error_response("Missing 'cdk_code' in request body")
            return

        try:
            result = validate_cdk_code(cdk_code=cdk_code, verbose=VERBOSE)
            
            self._send_json_response({
                "status": "success",
                "validation_output": result["validation_output"],
            })
        except Exception as e:
            self._send_internal_error(e)

    def _handle_analyze(self, data: dict[str, Any]) -> None:
        """
        Handle /analyze endpoint - analyze requirements without generating code.
        
        Request body:
        {
            "requirements": {...} or "text requirements"
        }
        """
        requirements = data.get("requirements")
        if not requirements:
            self._send_error_response("Missing 'requirements' in request body")
            return

        try:
            agent = create_agent(verbose=VERBOSE)
            
            # Format requirements
            if isinstance(requirements, dict):
                req_str = json.dumps(requirements, indent=2)
            else:
                req_str = str(requirements)
            
            query = f"""Analyze these infrastructure requirements and provide:
1. Parsed requirements (structured format)
2. Recommended CDK stacks
3. Any clarifying questions needed

Requirements:
{req_str}

Do NOT generate code yet, just analyze and provide recommendations.
"""
            
            result = agent.invoke({"messages": [("user", query)]})
            messages = result.get("messages", [])
            final_output = messages[-1].content if messages else ""
            
            self._send_json_response({
                "status": "success",
                "analysis": final_output,
            })
        except Exception as e:
            self._send_internal_error(e)

    def log_message(self, format: str, *args: Any) -> None:
        """Override to customize logging."""
        if VERBOSE:
            print(f"[Module3] {format % args}")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def run_server(port: int = PORT, host: str = HOST) -> None:
    """
    Run the Module 3 HTTP server.

    Parameters
    ----------
    port : int
        Port to listen on. Default 8082.
    host : str
        Host to bind to. Default "127.0.0.1" (localhost only).
        Set MODULE3_HOST=0.0.0.0 to bind to all interfaces.
    """
    server = HTTPServer((host, port), Module3Handler)
    if not _is_loopback(host):
        print("  WARNING: no authentication; reachable from your network")
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  Module 3: CDK Infrastructure Generation Agent                  ║
╚══════════════════════════════════════════════════════════════════╝

  Server running on http://{host}:{port}

  Endpoints:
    GET  /ping              - Health check
    POST /analyze           - Analyze infrastructure requirements
    POST /generate          - Generate CDK infrastructure code
    POST /validate          - Validate CDK code

  Example:
    curl -X POST http://localhost:{port}/generate \\
      -H "Content-Type: application/json" \\
      -d '{{"requirements": {{"compute": "ECS", "database": "RDS"}}, "region": "us-east-1"}}'

  Press Ctrl+C to stop
""")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nShutting down server...")
        server.shutdown()


if __name__ == "__main__":
    run_server()
