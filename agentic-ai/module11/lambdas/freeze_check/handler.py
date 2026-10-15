# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-freeze-check: the Approval stage's runOrder-1 action.

Reads SSM `/m11/devops-companion/freeze` (JSON `{"state": "none|planned|emergency", "reason": ...}`;
an absent parameter means `none`):
- `none`: PutJobSuccessResult; the manual approval follows.
- `planned`: hold. PutJobSuccessResult with a continuationToken, so CodePipeline keeps the action in
  progress and invokes it again until the freeze lifts or the action's limit fails it
  (FreezeTimeout). QUEUED mode holds later executions behind it.
- `emergency`: PutJobFailureResult (the execution fails at Approval).
Anything unreadable fails closed (PutJobFailureResult).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

PARAMETER = os.environ.get("FREEZE_PARAMETER", "/m11/devops-companion/freeze")
STATES = ("none", "planned", "emergency")


def read_freeze(ssm: Any, name: str = PARAMETER) -> dict:
    try:
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 - ParameterNotFound means no freeze; anything else fails closed
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code == "ParameterNotFound":
            return {"state": "none"}
        raise
    doc = json.loads(value) if value.strip().startswith("{") else {"state": value.strip()}
    if doc.get("state") not in STATES:
        raise ValueError("freeze parameter holds an unknown state")
    return doc


def decide(freeze: dict, continuation: str | None) -> tuple[str, str]:
    """('success' | 'hold' | 'failure', message)."""
    state = freeze.get("state", "none")
    if state == "none":
        return "success", "no freeze" + (" (lifted)" if continuation else "")
    if state == "planned":
        return "hold", f"planned freeze: {freeze.get('reason') or 'no reason given'}; holding at Approval"
    return "failure", f"emergency freeze: {freeze.get('reason') or 'no reason given'}"


def handler(event: dict, context: Any = None, *, make: Callable[[str], Any] | None = None) -> dict:
    import boto3  # Lambda runtime

    make = make or boto3.client
    job = event["CodePipeline.job"]
    job_id = job["id"]
    continuation = (job.get("data") or {}).get("continuationToken")
    pipeline = make("codepipeline")
    try:
        verdict, message = decide(read_freeze(make("ssm")), continuation)
    except Exception as exc:  # noqa: BLE001 - fail closed
        pipeline.put_job_failure_result(jobId=job_id, failureDetails={
            "type": "JobFailed", "message": f"freeze check failed closed: {type(exc).__name__}"[:500]})
        return {"verdict": "failure"}
    if verdict == "success":
        pipeline.put_job_success_result(jobId=job_id, executionDetails={"summary": message[:2048]})
    elif verdict == "hold":
        pipeline.put_job_success_result(jobId=job_id, continuationToken="frozen",
                                        executionDetails={"summary": message[:2048]})
    else:
        pipeline.put_job_failure_result(jobId=job_id, failureDetails={"type": "JobFailed", "message": message[:500]})
    print(json.dumps({"jobId": job_id, "verdict": verdict, "message": message}))
    return {"verdict": verdict}
