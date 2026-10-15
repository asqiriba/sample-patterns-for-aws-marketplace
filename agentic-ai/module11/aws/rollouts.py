# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Commands to a stage's rollback controller Lambda.

`release deploy`, `module11 rollout tick|abort|reset`, `latch clear` checks and the traffic driver never
write the control item themselves in dev, staging and prod: they invoke `m11-<stage>-controller`
synchronously. Its reserved concurrency is 1, so a second concurrent invocation is throttled; `invoke`
retries a throttle every 2 s (up to 120 s), which is how concurrent `rollout tick` calls serialize.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from module11.release import naming

THROTTLE_RETRY_SECONDS = 2.0
THROTTLE_LIMIT_SECONDS = 120.0
READ_TIMEOUT_SECONDS = 75


class ControllerError(RuntimeError):
    """The controller answered ok=false (invalid_transition, revision_mismatch, conflict) or failed."""

    def __init__(self, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.result = result or {}


def function_name(stage: str) -> str:
    return naming.function(stage, "controller")


def lambda_client(session: Any = None, region: str | None = None) -> Any:
    from botocore.config import Config
    config = Config(read_timeout=READ_TIMEOUT_SECONDS, retries={"max_attempts": 1, "mode": "standard"})
    if session is not None:
        return session.client("lambda", region_name=region, config=config)
    from . import client
    return client("lambda", region_name=region, config=config)


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def invoke(lam: Any, stage: str, command: str, args: dict | None = None, *, expected_revision: int | None = None,
           check: bool = True, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
           log: Callable[[str], None] = lambda line: None) -> dict:
    """One synchronous controller command; returns its result dict. `check` raises ControllerError on ok=false."""
    event: dict[str, Any] = {"command": command, "args": dict(args or {})}
    if expected_revision is not None:
        event["expectedRevision"] = int(expected_revision)
    started, throttles = clock(), 0
    while True:
        try:
            response = lam.invoke(FunctionName=function_name(stage), InvocationType="RequestResponse",
                                  Payload=json.dumps(event).encode("utf-8"))
            break
        except Exception as exc:  # noqa: BLE001
            if _code(exc) != "TooManyRequestsException" or clock() - started >= THROTTLE_LIMIT_SECONDS:
                raise
            throttles += 1
            if throttles == 1:
                log(f"controller busy (reserved concurrency 1): waiting to run `{command}`")
            sleep(THROTTLE_RETRY_SECONDS)
    payload = response["Payload"].read()
    if response.get("FunctionError"):
        raise ControllerError(f"controller {command} failed: {payload.decode('utf-8', 'replace')[:500]}")
    result = json.loads(payload or b"{}")
    result.setdefault("throttles", throttles)
    if check and not result.get("ok"):
        raise ControllerError(f"controller {command}: {result.get('error')} {result.get('detail') or ''}".strip(), result)
    return result
