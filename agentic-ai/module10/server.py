"""Small standard-library HTTP server for the Module 10 exposed agent."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from .models import ReviewRequest
from .runtime import build_runtime

MAX_BODY_BYTES = 128 * 1024


class ExposureRequestHandler(BaseHTTPRequestHandler):
    """HTTP adapter around :class:`module10.exposure.ExposureService`."""

    service: Any = None
    runtime_statuses: list[Any] = []
    runtime_mode: str = "mock"

    def _send_json(self, status_code: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            self._send_json(200, {"status": "ok", "service": "module10-exposure"})
            return
        if self.path == "/status":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "module10-exposure",
                    "mode": self.runtime_mode,
                    **(self.service.status() if self.runtime_mode == "local" else {}),
                    "components": [
                        {
                            "component": item.component,
                            "mode": item.mode,
                            "detail": item.detail,
                        }
                        for item in self.runtime_statuses
                    ],
                },
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invoke":
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("request body is required")
            if length > MAX_BODY_BYTES:
                self._send_json(
                    413,
                    {"error": f"request body exceeds {MAX_BODY_BYTES} bytes"},
                )
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            if self.runtime_mode == "local":
                from .reviewer import TeamReviewRequest

                teams = self.headers.get_all("X-Demo-Team", [])
                if len(teams) != 1:
                    raise ValueError("exactly one X-Demo-Team header is required")
                users = self.headers.get_all("X-Demo-User", [])
                if len(users) > 1:
                    raise ValueError("at most one X-Demo-User header is allowed")
                request = TeamReviewRequest.from_http(payload, teams[0], users[0] if users else None)
            else:
                request = ReviewRequest.from_dict(payload)
            response = self.service.review(request)
            self._send_json(response.status_code, response.body)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json(
                500,
                {
                    "error": "internal server error",
                    "detail": type(exc).__name__,
                },
            )

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def create_server(
    service: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8090,
    runtime_mode: str = "mock",
    runtime_statuses: list[Any] | None = None,
) -> ThreadingHTTPServer:
    # The endpoint has no authentication in any mode, so it never binds beyond
    # loopback. The deployed cloud path is module10/cloud_app.py, not this server.
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The unauthenticated Module 10 demo endpoint must bind to localhost")
    handler = type(
        "BoundExposureRequestHandler",
        (ExposureRequestHandler,),
        {
            "service": service,
            "runtime_mode": runtime_mode,
            "runtime_statuses": runtime_statuses or [],
        },
    )
    return ThreadingHTTPServer((host, port), handler)


def start_server(
    service: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    runtime_mode: str = "mock",
    runtime_statuses: list[Any] | None = None,
) -> tuple[ThreadingHTTPServer, Thread]:
    server = create_server(
        service,
        host=host,
        port=port,
        runtime_mode=runtime_mode,
        runtime_statuses=runtime_statuses,
    )
    thread = Thread(target=server.serve_forever, name="module10-http", daemon=True)
    thread.start()
    return server, thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Module 10 exposure endpoint")
    parser.add_argument(
        "--host",
        default=os.getenv("MODULE10_HOST", "127.0.0.1"),
        help=(
            "loopback address to bind (127.0.0.1 or localhost); defaults to "
            "MODULE10_HOST or 127.0.0.1. The endpoint has no authentication, so "
            "other addresses are refused in every mode"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MODULE10_PORT", "8090")),
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "local", "live"),
        default=None,
        help="runtime mode; defaults to MODULE10_MODE or mock",
    )
    args = parser.parse_args()

    runtime = build_runtime(args.mode)
    server = create_server(
        runtime.service,
        host=args.host,
        port=args.port,
        runtime_mode=runtime.mode,
        runtime_statuses=runtime.statuses,
    )
    print(f"Module 10 endpoint listening on http://{args.host}:{args.port}")
    print("POST /invoke | GET /ping | GET /status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Module 10 endpoint.")
    finally:
        server.server_close()
        runtime.close()


if __name__ == "__main__":
    main()
