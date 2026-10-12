"""Real local Portkey routing for the Companion's native Responses tool loops."""

from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Queue, Empty
import shutil
import subprocess
from threading import Thread
import time
import urllib.request
import urllib.parse
import uuid

from .agent_runner import AgentRunError
from .control_planes import IntegrationUnavailable
from .gateway.setup import REVISION, SOURCE
from .reviewer import BedrockReviewer
from .urls import open_url

GATEWAY_DIR = Path(__file__).with_name("gateway")


class LocalPortkeyProcess:
    """Own one loopback gateway process; drain its output without logging secrets."""

    def __init__(self, launcher: Path | None = None) -> None:
        loader = SOURCE / "node_modules/tsx/dist/loader.mjs"
        if not shutil.which("node") or not loader.exists():
            raise IntegrationUnavailable(
                "Run .venv/bin/python module10/gateway/setup.py to install local Portkey."
            )
        revision = subprocess.check_output(
            ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True,
        ).strip()
        if revision != REVISION:
            raise IntegrationUnavailable("The local Portkey checkout does not match the pinned revision.")
        self.process = subprocess.Popen(
            ["node", "--import", str(loader), str(launcher or GATEWAY_DIR / "launcher.mjs"), str(SOURCE)],
            cwd=SOURCE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        ready = Queue()

        def drain():
            startup = []
            announced = False
            for line in self.process.stdout:
                if not announced:
                    startup.append(line.strip())
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("ready"):
                        ready.put(message)
                        announced = True
                        startup.clear()
            if not announced:
                ready.put(RuntimeError("Portkey startup failed: " + "\n".join(startup[-12:])))

        self.reader = Thread(target=drain, name="module10-portkey-output", daemon=True)
        self.reader.start()
        try:
            result = ready.get(timeout=30)
            if isinstance(result, Exception):
                raise IntegrationUnavailable(str(result))
            self.base_url = result["base_url"]
            self.console_url = result["console_url"]
        except Empty:
            self.close()
            raise IntegrationUnavailable("Portkey startup timed out; rerun module10/gateway/setup.py.") from None
        except Exception:
            self.close()
            raise

    def set_drill(self, armed: bool) -> dict:
        request = urllib.request.Request(
            self.base_url + "/__demo/drill",
            data=json.dumps({"armed": armed}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with open_url(request, timeout=5) as response:
            return json.load(response)

    def console_evidence(self, records=None):
        """Authenticated loopback transfer of observed, credential-free logs."""
        token = urllib.parse.parse_qs(urllib.parse.urlsplit(self.console_url).fragment)["token"][0]
        request = urllib.request.Request(
            self.base_url + "/__demo/console-evidence",
            data=json.dumps(records).encode() if records is not None else None,
            headers={"Content-Type": "application/json", "X-Module10-Console-Token": token},
        )
        with open_url(request, timeout=5) as response:
            return json.load(response)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()


class PortkeyReviewer(BedrockReviewer):
    def __init__(self, client, gateway: LocalPortkeyProcess, session) -> None:
        super().__init__(client, "devops-companion")
        self.gateway = gateway
        self.session = session

    @classmethod
    def from_env(cls) -> "PortkeyReviewer":
        import botocore.session
        import httpx2
        from openai import OpenAI

        session = botocore.session.get_session()
        if not (os.getenv("AWS_REGION") or session.get_config_variable("region")):
            raise IntegrationUnavailable("Set AWS_REGION or configure a region in your AWS profile.")
        gateway = LocalPortkeyProcess()
        try:
            client = OpenAI(
                # Local SDK placeholder; the launcher removes this header.
                api_key="local-demo",
                base_url=gateway.base_url + "/v1/openai/v1",
                max_retries=0, timeout=60,
                http_client=httpx2.Client(trust_env=False),
            )
            return cls(client, gateway, session)
        except Exception:
            gateway.close()
            raise

    def create_response(self, *, agent, evidence_ready, record, deadline, **params):
        from botocore.exceptions import NoCredentialsError
        from openai import APIStatusError

        roles = {"DevOps Companion": "devops-companion", "Recovery Planner": "recovery-planner"}
        role = roles[agent]
        credentials = self.session.get_credentials()
        if credentials is None:
            raise NoCredentialsError()
        # Refresh on each model turn; neither the profile nor keys are persisted.
        credentials = credentials.get_frozen_credentials()
        region = os.getenv("AWS_REGION") or self.session.get_config_variable("region")
        remaining = min(60.0, deadline - time.monotonic())
        if remaining <= 2:
            raise AgentRunError("Insufficient time remains for a model request")
        target_timeout_ms = int(min(25.0, (remaining - 2) / 2) * 1000)
        try:
            config = json.loads((GATEWAY_DIR / "routing.json").read_text())
        except json.JSONDecodeError as exc:
            raise AgentRunError("The local Portkey routing policy is invalid JSON") from exc

        def configure(target):
            target["request_timeout"] = target_timeout_ms
            if target.get("provider") == "bedrock":
                target.update({
                    "aws_region": region,
                    "aws_access_key_id": credentials.access_key,
                    "aws_secret_access_key": credentials.secret_key,
                    "aws_session_token": credentials.token or "",
                })
            for child in target.get("targets", []):
                configure(child)

        configure(config)
        from .observability import correlation

        call_id = record.get("call_id") or uuid.uuid4().hex
        record.update({
            "call_id": call_id, "requested_model": role,
            "evidence_ready": evidence_ready, "attempts": [], "fallback_used": False,
        })
        params.update(
            model=role,
            timeout=remaining,
            extra_headers={
                "x-portkey-config": json.dumps(config),
                "x-portkey-trace-id": call_id,
                "x-portkey-metadata": json.dumps({
                    "agent_role": role, "evidence_ready": str(evidence_ready).lower(),
                    **correlation(),
                }),
            },
        )

        def capture(headers):
            attempts = json.loads(headers.get("x-module10-attempts", "[]"))
            record.update({
                "attempts": attempts,
                "gateway_target": headers.get("x-portkey-last-used-option-index"),
                "fallback_used": len(attempts) > 1 and 200 <= attempts[-1]["status_code"] < 300,
            })

        try:
            raw = self.client.responses.with_raw_response.create(**params)
        except APIStatusError as exc:
            capture(exc.response.headers)
            raise
        capture(raw.headers)
        if not record["attempts"] or not record["gateway_target"]:
            raise AgentRunError("Portkey returned no verifiable routing evidence")
        return raw.parse()

    def close(self) -> None:
        try:
            self.client.close()
        finally:
            self.gateway.close()
