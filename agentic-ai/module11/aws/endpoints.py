# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Runtime endpoint retargets.

No boto3 waiter exists, so `wait` polls every 5 s with a 10-minute deadline. Endpoints omit
`targetVersion` when idle, so the settled rule is: READY and liveVersion == intended and
(targetVersion absent or targetVersion == intended). A ConflictException (an update already in
progress) waits for a non-UPDATING status and retries the retarget 3 times, 5 s apart (N5).
"""
from __future__ import annotations

import time
from typing import Any, Callable

POLL_SECONDS = 5
DEADLINE_SECONDS = 600
CONFLICT_RETRIES = 3


class EndpointUpdateFailed(RuntimeError):
    pass


class EndpointVersionMismatch(RuntimeError):
    pass


class EndpointTimeout(EndpointUpdateFailed):
    pass


def settled(endpoint: dict, intended: str | int) -> bool:
    target = endpoint.get("targetVersion")
    return (endpoint.get("status") == "READY" and str(endpoint.get("liveVersion")) == str(intended)
            and (target is None or str(target) == str(intended)))


def get(ctl: Any, runtime_id: str, name: str) -> dict:
    return ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)


def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def wait(ctl: Any, runtime_id: str, name: str, intended: str | int, *, interval: float = POLL_SECONDS,
         deadline: float = DEADLINE_SECONDS, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> dict:
    """Poll until settled. UPDATE_FAILED raises EndpointUpdateFailed(failureReason); READY with a
    different live version and no target raises EndpointVersionMismatch."""
    end = clock() + deadline
    while True:
        endpoint = get(ctl, runtime_id, name)
        status = endpoint.get("status")
        if settled(endpoint, intended):
            return endpoint
        if status in {"UPDATE_FAILED", "CREATE_FAILED"}:
            raise EndpointUpdateFailed(f"{name}: {status}: {endpoint.get('failureReason') or 'no reason given'}")
        if status == "READY" and endpoint.get("targetVersion") is None and \
                str(endpoint.get("liveVersion")) != str(intended):
            raise EndpointVersionMismatch(f"{name}: live {endpoint.get('liveVersion')} != intended {intended}")
        if clock() >= end:
            raise EndpointTimeout(f"{name}: not READY on {intended} within {int(deadline)} s (status {status})")
        sleep(interval)


def _wait_not_updating(ctl: Any, runtime_id: str, name: str, *, sleep, clock, deadline: float) -> None:
    end = clock() + deadline
    while get(ctl, runtime_id, name).get("status") == "UPDATING":
        if clock() >= end:
            raise EndpointTimeout(f"{name}: still UPDATING after {int(deadline)} s")
        sleep(POLL_SECONDS)


def retarget(ctl: Any, runtime_id: str, name: str, version: str | int, *,
             sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
             deadline: float = DEADLINE_SECONDS) -> dict:
    """UpdateAgentRuntimeEndpoint, then wait for the settled rule."""
    for attempt in range(CONFLICT_RETRIES + 1):
        try:
            ctl.update_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name,
                                              agentRuntimeVersion=str(version))
            break
        except Exception as exc:
            if _code(exc) != "ConflictException" or attempt == CONFLICT_RETRIES:
                raise
            _wait_not_updating(ctl, runtime_id, name, sleep=sleep, clock=clock, deadline=deadline)
            sleep(POLL_SECONDS)
    return wait(ctl, runtime_id, name, version, sleep=sleep, clock=clock, deadline=deadline)


def versions(ctl: Any, runtime_id: str) -> list[str]:
    """Runtime versions, newest first (ListAgentRuntimeVersions, paged)."""
    out, token = [], None
    while True:
        kwargs = {"agentRuntimeId": runtime_id, **({"nextToken": token} if token else {})}
        page = ctl.list_agent_runtime_versions(**kwargs)
        out += [str(v["agentRuntimeVersion"]) for v in page.get("agentRuntimes", [])]
        token = page.get("nextToken")
        if not token:
            return sorted(set(out), key=int, reverse=True)
