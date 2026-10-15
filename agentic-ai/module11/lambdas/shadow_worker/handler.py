# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shadow worker: an SQS consumer on `m11-<stage>-shadow`.

For each message the m11 adapter enqueued while shadow sampling is on, it re-reads the control item
and, only while that rollout is still in a shadow state, invokes the `candidate` endpoint with
`variant=shadow, shadow=true` (side effects suppressed in the runtime), the message's candidate
identity in the envelope, session `m11-<stagekey>-shadow-v<candidate>-<uuid4>` and no traceparent. It
then writes the shadow outcome item `{pk: ROLLOUT#<id>, sk: <epochMs>#shadow-<originalRequestId>,
shadow: true, originalRequestId, runtimeSessionId}` so the controller can pair it with the original
stable session, and stops the runtime session. A failed record is reported back to SQS
(partial batch response) and retried.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from module11.aws import control_table as CT

SHADOW_STATES = ("SHADOWING", "WARMING", "OBSERVING", "ADVANCING")
OUTCOME_TTL_SECONDS = 30 * 86400


def _client(name: str) -> Any:
    from module11 import aws
    return aws.client(name)


def envelope(message: dict, request_id: str) -> dict:
    candidate = message["candidate"]
    return {"identity": {"team": message["team"], "principal": message["principal"]},
            "request": {"change_summary": message["change_summary"], "request_id": request_id},
            "context": {"variant": "shadow", "shadow": True, "conversationId": message["conversationId"],
                        "rolloutId": message["rolloutId"], "manifestVersion": candidate["manifestVersion"],
                        "behaviorDigest": candidate["behaviorDigest"], "release": candidate["release"]}}


def shadow_outcome(message: dict, *, session: str, version: Any, status: str, started_ms: int, now_ms: int) -> dict:
    candidate = message["candidate"]
    return {"pk": f"ROLLOUT#{message['rolloutId']}", "sk": f"{now_ms}#shadow-{message['originalRequestId']}",
            "arm": "candidate", "qualifier": "candidate", "shadow": True, "runtimeVersion": str(version),
            "runtimeSessionId": session, "originalRequestId": message["originalRequestId"],
            "conversationId": message["conversationId"], "manifestVersion": candidate["manifestVersion"],
            "behaviorDigest": candidate["behaviorDigest"], "release": candidate["release"], "team": message["team"],
            "status": status, "latencyMs": now_ms - started_ms, "expiresAt": now_ms // 1000 + OUTCOME_TTL_SECONDS}


def _status(text: str) -> str:
    for block in reversed(text.replace("\r\n", "\n").split("\n\n")):
        if block.startswith("event: result") or block.startswith("event: error"):
            try:
                return str(json.loads(block.split("data: ", 1)[1]).get("status") or "failed")
            except (IndexError, ValueError):
                return "failed"
    return "failed"


def process(message: dict, *, env: dict, clients: dict, clock=time.time) -> str:
    ddb, core = clients["dynamodb"], clients["bedrock-agentcore"]
    state = CT.get(ddb, env["CONTROL_TABLE"]) or {}
    if state.get("rolloutId") != message.get("rolloutId") or state.get("state") not in SHADOW_STATES:
        return "skipped"                                    # the rollout moved on: drop the work item
    version = (state.get("endpoints") or {}).get("candidate", {}).get("version")
    session = f"m11-{env['SESSION_STAGE']}-shadow-v{int(version)}-{uuid.uuid4()}"
    started_ms = int(clock() * 1000)
    request_id = f"shadow-{message['originalRequestId']}"[:128]
    status = "failed"
    try:
        response = core.invoke_agent_runtime(
            agentRuntimeArn=env["RUNTIME_ARN"], qualifier="candidate", runtimeSessionId=session,
            contentType="application/json", accept="text/event-stream",
            payload=json.dumps(envelope(message, request_id)).encode("utf-8"))
        status = _status(response["response"].read().decode("utf-8", "replace"))
    finally:
        CT.put_event(ddb, env["OUTCOMES_TABLE"], shadow_outcome(
            message, session=session, version=version, status=status, started_ms=started_ms,
            now_ms=int(clock() * 1000)))
        try:
            core.stop_runtime_session(agentRuntimeArn=env["RUNTIME_ARN"], qualifier="candidate", runtimeSessionId=session)
        except Exception as exc:  # noqa: BLE001 - the 60 s idle timeout ends it anyway
            print(json.dumps({"level": "WARN", "event": "stop_session_failed", "error_type": type(exc).__name__}))
    return status


def handler(event: dict, context: Any = None) -> dict:
    env = dict(os.environ)
    clients = {name: _client(name) for name in ("dynamodb", "bedrock-agentcore")}
    failures = []
    for record in event.get("Records") or []:
        try:
            outcome = process(json.loads(record["body"]), env=env, clients=clients)
            print(json.dumps({"level": "INFO", "event": "shadow", "messageId": record.get("messageId"),
                              "outcome": outcome}))
        except Exception as exc:  # noqa: BLE001 - reported per record; SQS retries it
            print(json.dumps({"level": "ERROR", "event": "shadow_failed", "messageId": record.get("messageId"),
                              "error_type": type(exc).__name__}))
            failures.append({"itemIdentifier": record.get("messageId")})
    return {"batchItemFailures": failures}
