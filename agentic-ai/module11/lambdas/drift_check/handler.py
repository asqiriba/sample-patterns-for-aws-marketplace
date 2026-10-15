# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The drift checker Lambda `m11-<stage>-drift-check`: the I/O around `release/drift.py`.

EventBridge Scheduler invokes it every 15 minutes (`rate(15 minutes)`) in dev, staging and prod; `module11 drift
check --stage <s>` invokes it synchronously. Either way it reads the stage's deployed manifest (`/current`,
`/candidate` during a rollout, `/applied`), the release ledger by prefix listing, and the live state (endpoints,
pinned runtime versions, the bundle and its versions, tool functions, role policies, the guardrail, the flag
object), then writes the `DriftReport` to SSM `/m11/devops-companion/<stage>/drift`, the CloudWatch metric
`m11/Drift DriftFindings` (Stage dimension) and its log. There is no auto-revert (`module11 drift revert`).
Read-only except for those two writes. `mr-*` stacks have no drift checker.

Event: `{}` or `{"source": "cli"}`. Result: `{ok, report, driftCount, seconds}` or `{ok: false, error, detail}`.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any


def _account(context: Any) -> str:
    arn = getattr(context, "invoked_function_arn", "") or ""
    parts = arn.split(":")
    if len(parts) > 4 and parts[4]:
        return parts[4]
    import boto3
    return boto3.client("sts").get_caller_identity()["Account"]


def log(level: str, event: str, **fields: Any) -> None:
    print(json.dumps({"level": level, "event": event, "stage": os.environ.get("M11_STAGE"), **fields}, default=str),
          flush=True)


def handler(event: dict | None, context: Any = None) -> dict:
    import boto3
    from module11.aws import drift_reads
    stage = os.environ["M11_STAGE"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    session = boto3.session.Session(region_name=region)
    started = time.time()
    try:
        report = drift_reads.check(stage=stage, admin=session, account=_account(context), region=region,
                                   release_bucket=os.environ["RELEASE_BUCKET"])
    except Exception as exc:  # a failed read is not "no drift": report it and let the alarm or the CLI see it
        log("ERROR", "drift_check_failed", error=type(exc).__name__, detail=str(exc)[:500])
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}
    drift_reads.publish(session, report)
    seconds = round(time.time() - started, 1)
    log("WARN" if report.drift_count else "INFO", "drift_report", driftCount=report.drift_count,
        findings=[f.text() for f in report.findings], source=(event or {}).get("source", "schedule"), seconds=seconds,
        endpoints=sorted({f.field for f in report.findings if f.component == "endpoint"}))
    return {"ok": True, "report": report.to_dict(), "driftCount": report.drift_count, "seconds": seconds}
