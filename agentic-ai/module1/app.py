# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
app.py
======
Amazon Bedrock AgentCore Runtime entrypoint.

This file wraps the Strands agent in a BedrockAgentCoreApp, converting it
into a production-ready HTTP service with:
  - POST /invocations  : invoke the agent
  - GET  /ping         : health check
  - Session isolation  : each user session runs in a dedicated microVM
  - Auto-scaling       : handles thousands of concurrent sessions
  - CloudWatch logging : built-in observability

MODULE 1 CONCEPT: The Deployment-Logic Separation
---------------------------------------------------
Notice how little code is here:

    from bedrock_agentcore import BedrockAgentCoreApp
    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload):
        return agent(payload["prompt"])

    app.run()

The agent logic (agent.py) is completely unchanged when deploying to
AgentCore. This is the same abstraction arc the Module 1 slides describe:
your agent IS the application; AgentCore is the managed runtime platform.

DEPLOYMENT STEPS (run every command from agentic-ai/)
-----------------------------------------------------
  # 1. Install the starter toolkit CLI
  pip install bedrock-agentcore-starter-toolkit

  # 2. Configure deployment (run once)
  agentcore configure --entrypoint module1/app.py --disable-memory

  # 3. Test locally (no Docker needed — serves on http://localhost:8080)
  AGENT_MOCK_AWS=true python -m module1.app

  # 4. Test the local server
  curl -X POST http://localhost:8080/invocations \\
    -H "Content-Type: application/json" \\
    -d '{"prompt": "Give me a health summary of us-east-1"}'

  # 5. Deploy to AWS AgentCore Runtime (CodeBuild, no Docker needed)
  agentcore deploy          # formerly `agentcore launch`

  # 6. Invoke the deployed agent
  agentcore invoke '{"prompt": "List ECS services in us-east-1"}'

  # 7. Invoke via boto3 (for integration with other services)
  #    See: scripts/invoke_agentcore.py

  Note: the Starter Toolkit CLI now reports that it is no longer supported.
  For new projects AWS recommends the AgentCore CLI (@aws/agentcore):
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html
  These steps were not re-verified by deployment in the Module 11
  maintenance release.

MOCK MODE
----------
Set AGENT_MOCK_AWS=true to run without real AWS credentials.
The agent responds with realistic simulated data — good for demos.

  AGENT_MOCK_AWS=true python -m module1.app

LOCAL FALLBACK SERVER
----------------------
Without bedrock-agentcore installed, the plain HTTP fallback binds to
127.0.0.1 by default and has no authentication; local demo use only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys

# Ensure the project root (agentic-ai/, the parent of module1/) is on the
# path when running directly as `python module1/app.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from module1.agent import create_agent

# ---------------------------------------------------------------------------
# AgentCore import with graceful fallback
# ---------------------------------------------------------------------------
# The fallback ensures the file runs (with a plain HTTP server) even if
# bedrock-agentcore hasn't been installed yet — useful for initial setup.

try:
    from bedrock_agentcore import BedrockAgentCoreApp
    _AGENTCORE = True
except ImportError:
    _AGENTCORE = False
    print("⚠  bedrock-agentcore not installed — running plain HTTP fallback.")
    print("   Install: pip install bedrock-agentcore bedrock-agentcore-starter-toolkit")


# ---------------------------------------------------------------------------
# Shared agent instance (created once at startup, reused across requests)
# ---------------------------------------------------------------------------

_hf_arn = os.getenv("HF_ENDPOINT_ARN")

print("\n  Initialising AWS Infrastructure Agent...")
_agent = create_agent(
    hf_endpoint_arn=_hf_arn if _hf_arn else None,
    verbose=False,   # suppress loop steps in server mode (use CloudWatch instead)
)
print("  Agent ready.\n")


# ---------------------------------------------------------------------------
# Request handler (shared between AgentCore and fallback)
# ---------------------------------------------------------------------------

# ASCII digits only ([0-9], not \d, which also matches non-ASCII digits).
_REGION_RE = re.compile(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]")
_INVALID_REGION = {"error": "invalid region"}


def _handle(payload: dict) -> dict:
    """
    Process an incoming agent request.

    Expected payload
    ----------------
    {
      "prompt"  : str   — required: the user's instruction
      "region"  : str   — optional: override AWS region for this request
      "verbose" : bool  — optional: print loop steps to console (local dev)
    }
    """
    prompt = payload.get("prompt", "").strip()
    if not prompt:
        return {"error": "Missing required field: 'prompt'"}

    region = payload.get("region")
    if region:
        # AWS_REGION is process-wide, so only a well-formed Region name may
        # be written to it.
        if not isinstance(region, str) or not _REGION_RE.fullmatch(region):
            return dict(_INVALID_REGION)
        os.environ["AWS_REGION"] = region

    # Use a verbose agent for local testing if requested
    if payload.get("verbose"):
        local_agent = create_agent(
            hf_endpoint_arn=_hf_arn if _hf_arn else None,
            verbose=True,
        )
        response = local_agent(prompt)
    else:
        response = _agent(prompt)

    return {
        "result": str(response),
        "region": os.getenv("AWS_REGION", "us-east-1"),
        "mock_mode": os.getenv("AGENT_MOCK_AWS", "false").lower() == "true",
        "model": "huggingface-bedrock-marketplace" if _hf_arn else "claude-sonnet-4-6-bedrock",
    }


# ---------------------------------------------------------------------------
# AgentCore entrypoint
# ---------------------------------------------------------------------------

if _AGENTCORE:
    app = BedrockAgentCoreApp()

    @app.entrypoint
    def invoke(payload: dict) -> dict:
        """
        AgentCore entrypoint — called for every POST /invocations request.

        AgentCore deserialises the JSON body, calls this function, and
        serialises the return value back to JSON. The only AgentCore-specific
        code in this project is these ~4 lines.
        """
        return _handle(payload)


# ---------------------------------------------------------------------------
# Fallback: minimal HTTP server (no AgentCore installed)
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


def make_handler():
    """
    Build the fallback request handler class.

    Binds to 127.0.0.1 by default and has no authentication; local demo use
    only. POST bodies must be application/json and at most MAX_BODY_BYTES.
    When bound to loopback, requests whose Host header is not a loopback
    name are refused (DNS-rebinding protection).
    """
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass  # suppress default access log

        def do_GET(self) -> None:
            if not self._host_allowed():
                return
            if self.path == "/ping":
                self._respond(200, {"status": "ok"})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._host_allowed():
                return
            if self.path != "/invocations":
                self._respond(404, {"error": "not found"})
                return
            body = self._read_json_body()
            if body is None:
                return
            try:
                result = _handle(body)
            except Exception as exc:  # generic 500: never echo exception text
                print(f"[Module1] {type(exc).__name__}: {exc}", file=sys.stderr)
                self._respond(500, {"error": "internal server error", "detail": type(exc).__name__})
                return
            self._respond(400 if result == _INVALID_REGION else 200, result)

        def _host_allowed(self) -> bool:
            """Refuse non-loopback Host headers when bound to loopback."""
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
            payload = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _run_fallback(host: str = "127.0.0.1", port: int = 8080) -> None:
    """
    Plain HTTP server that mirrors the AgentCore /invocations interface.

    Binds to 127.0.0.1 by default and has no authentication; local demo use only.
    """
    from http.server import HTTPServer

    Handler = make_handler()

    mock = os.getenv("AGENT_MOCK_AWS", "false").lower() == "true"
    if not _is_loopback(host):
        print("  WARNING: no authentication; reachable from your network")
    print(f"  🚀  Infrastructure Agent HTTP server")
    print(f"      http://{host}:{port}/invocations  (POST)")
    print(f"      http://{host}:{port}/ping         (GET)")
    print(f"      Mock mode : {'ON — no AWS credentials needed' if mock else 'OFF — using live AWS'}")
    print(f"\n  Example:")
    print(f"    curl -X POST http://localhost:{port}/invocations \\")
    print(f"      -H 'Content-Type: application/json' \\")
    print(f"      -d '{{\"prompt\": \"Give me a health summary of us-east-1\"}}'")
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
    if _AGENTCORE:
        print("  🚀  Starting via Bedrock AgentCore Runtime...")
        app.run()
    else:
        _run_fallback()
