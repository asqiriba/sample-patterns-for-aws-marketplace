"""AgentCore HTTP adapter for the existing Companion/Planner scenario.

Only trusted IAM callers may submit the internal identity envelope. The public
API adapter constructs it after authorization; it is not the client contract.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

import boto3

from .reviewer import TeamReviewRequest
from .runtime import build_runtime

MAX_BODY = 128 * 1024


def validate_envelope(payload):
    if not isinstance(payload, dict) or set(payload) - {"identity", "request", "context"}:
        raise ValueError("Invalid internal invocation")
    identity = payload.get("identity")
    if not isinstance(identity, dict) or set(identity) != {"team", "principal"}:
        raise ValueError("Trusted identity is required")
    for name in ("team", "principal"):
        value = identity[name]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("Invalid identity")
    context = payload.get("context", {})
    if not isinstance(context, dict) or set(context) - {"capture_session", "section", "model_backend", "drill", "traceparent"}:
        raise ValueError("Invalid invocation context")
    if not isinstance(payload.get("request"), dict):
        raise ValueError("Request must be an object")
    request = TeamReviewRequest.from_http(payload["request"], identity["team"], identity["principal"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request.request_id):
        raise ValueError("Invalid request identifier")
    if context.get("model_backend", "bedrock") not in {"bedrock", "portkey"}:
        raise ValueError("Invalid model backend")
    context = {**context, "model_backend": "portkey"}
    if type(context.get("section", 1)) is not int or context.get("section", 1) not in {0, 1, 2}:
        raise ValueError("Invalid capture section")
    if context.get("capture_session") is not None:
        from .observability import validate_session
        validate_session(context["capture_session"])
    return request, context


def investigate(request, context):
    # Flags are invocation-local; all model calls use the external ECS gateway.
    with tempfile.TemporaryDirectory(prefix="module10-") as directory:
        flags = Path(directory) / "flags.json"
        bucket = os.getenv("MODULE10_ARTIFACT_BUCKET")
        flags_key = os.getenv("MODULE10_FLAGS_KEY")
        if flags_key:
            flags.write_bytes(boto3.client("s3").get_object(Bucket=bucket, Key=flags_key)["Body"].read())
        else:
            flags.write_text(Path(__file__).with_name("flags.json").read_text())
        from .control_planes import IntegrationUnavailable
        try:
            runtime = build_runtime("local", flags_path=flags,
                model_backend="portkey",
                session=context.get("capture_session"), section=context.get("section", 1),
                hosted_identity={"team": request.team, "principal": request.user_id,
                                 "request_id": request.request_id})
        except IntegrationUnavailable:
            # Persist a terminal outcome even when no agent could start. The
            # streaming adapter reads this artifact to mark the request FAILED.
            from .models import ReviewResponse
            body = {"status": "failed", "request_id": request.request_id,
                "team": request.team, "user_id": request.user_id, "artifact_id": request.request_id,
                "error": "Portkey environment is stopped or unavailable; start it before invoking agents.",
                "model_calls": []}
            if bucket:
                boto3.client("s3").put_object(Bucket=bucket,
                    Key=f"results/{request.team}/{request.user_id}/{request.request_id}.json",
                    Body=json.dumps(body).encode(), ContentType="application/json")
            return ReviewResponse(503, body)
        # One capture group spans the section's transactions across isolated VMs.
        if context.get("capture_session"):
            runtime.telemetry.run_id = f"cloud-{context['capture_session']}-{context.get('section', 0)}"
        trace_token = None
        try:
            if context.get("traceparent"):
                from opentelemetry.context import attach
                from opentelemetry.propagate import extract
                trace_token = attach(extract({"traceparent": context["traceparent"]}))
            if context.get("drill") is True:
                runtime.model_gateway.gateway.set_drill(True)
            response = runtime.service.review(request)
            response.body["portkey_console_logs"] = runtime.model_gateway.gateway.console_evidence()
            runtime.telemetry.flush()
            if bucket:
                key = f"results/{request.team}/{request.user_id}/{request.request_id}.json"
                plan = response.body.get("recovery_plan")
                if plan:
                    plan.pop("path", None)
                response.body["artifact_id"] = request.request_id
                boto3.client("s3").put_object(Bucket=bucket, Key=key,
                    Body=json.dumps(response.body).encode(), ContentType="application/json")
                for record in runtime.telemetry.records:
                    metadata = {**record, "telemetry.export": runtime.telemetry.export_status}
                    boto3.client("s3").put_object(Bucket=bucket,
                        Key=f"manifests/{runtime.telemetry.session}/{record['trace_id']}.json",
                        Body=json.dumps(metadata).encode(), ContentType="application/json")
            return response
        finally:
            runtime.close()
            if trace_token is not None:
                from opentelemetry.context import detach
                detach(trace_token)


def start_collector():
    secret = os.getenv("MODULE10_TELEMETRY_SECRET")
    if not secret:
        return None
    settings = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=secret)["SecretString"])
    allowed = {"LOGZIO_REGION", "LOGZIO_LOGS_TOKEN", "LOGZIO_TRACES_TOKEN"}
    if set(settings) != allowed or any(not settings[k] for k in allowed):
        raise ValueError("Incomplete telemetry configuration")
    from .collector import CACHE
    binary = CACHE / "linux_arm64" / "otelcol-contrib"
    if not binary.is_file():
        raise RuntimeError("Collector must be installed in the image at build time")
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; the checksum-verified collector baked into the image, packaged config
    process = subprocess.Popen([str(binary), "--config", str(Path(__file__).with_name("collector.yaml"))],
        env={**os.environ, **settings}, stdout=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("Collector failed to start")
        try:
            with socket.create_connection(("127.0.0.1", 4318), timeout=0.1):
                os.environ["MODULE10_OTLP_ENDPOINT"] = "http://127.0.0.1:4318"
                return process
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError("Collector startup timed out")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        data = json.dumps({"status": "Healthy"} if self.path == "/ping" else {"error": "not found"}).encode()
        self.send_response(200 if self.path == "/ping" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/invocations":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid body size")
            request, context = validate_envelope(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError, AttributeError):
            self.send_error(400, "Invalid invocation")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def event(name, data):
            encoded = f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(f"{len(encoded):X}\r\n".encode() + encoded + b"\r\n")
            self.wfile.flush()

        try:
            event("accepted", {"request_id": request.request_id, "team": request.team})
            with ThreadPoolExecutor(max_workers=1) as pool:
                task = pool.submit(investigate, request, context)
                while True:
                    try:
                        response = task.result(timeout=10)
                        break
                    except TimeoutError:
                        event("heartbeat", {"request_id": request.request_id})
                event("result" if response.status_code == 200 else "error", response.body)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            # Do not return credential-bearing exception messages.
            from .control_planes import IntegrationUnavailable
            error = ("Portkey environment is stopped or unavailable; start it before invoking agents."
                     if isinstance(exc, IntegrationUnavailable) else type(exc).__name__)
            event("error", {"status": "failed", "error": error, "request_id": request.request_id})
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    collector = start_collector()
    try:
        ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # nosec B104 - AgentCore Runtime container: the service requires 0.0.0.0:8080 and fronts it
    finally:
        if collector:
            collector.terminate()
