# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run records and their S3 layout in the release bucket.

    evaluations/<stage>/<runId>.json                                a RunRecord (gate, calibration or rebaseline run)
    baselines/<manifestVersion>/<evaluatorSetVersion>/baseline.json  calibrated means per (evaluator, category, scope)
    baselines/<manifestVersion>/<evaluatorSetVersion>/noise.json     the run-to-run noise floor per the same keys
    calibrations/<calibrationId>/runs.json                           the run IDs of one calibration (resume)

A RunRecord is written by the caller's own role (the CodeBuild project role, or the operator), never by
the eval-runner role, which has no S3 access.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from module11.release import gate as G

SCHEMA = "m11.run/1"


@dataclass
class SessionRecord:
    scenarioId: str
    category: str
    tags: list[str]
    repeat: int
    sessionId: str
    status: str = "failed"              # completed | failed | blocked | invoke_error
    detail: str | None = None
    turns: int = 0
    latencyMs: float = 0.0
    inputTokens: int = 0
    outputTokens: int = 0
    toolCalls: int = 0
    tools: list[str] = field(default_factory=list)
    blocked: bool = False
    retried: bool = False
    startedAt: float = 0.0
    endedAt: float = 0.0
    guardBlocked: int = 0               # tool calls the runtime's guards refused (e.g. delegation before history)
    invokeRetries: int = 0              # InvokeAgentRuntime calls re-sent after a throttle (evaluation/provider_health.py)
    modelRetries: dict[str, int] = field(default_factory=dict)   # the runtime's model-call retries by class
    reinvokeOf: str | None = None       # the session ID this transport re-invoke replaced (runner.transport_reinvokes)
    spans: str = "pending"              # ready | spans_missing | spans_too_large | spans_fetch_failed | not_invoked
    traceIds: list[str] = field(default_factory=list)
    requestIds: list[str] = field(default_factory=list)


@dataclass
class RunRecord:
    runId: str
    kind: str                           # gate | calibration | rebaseline | test
    stage: str
    endpoint: str
    profile: str
    scope: str
    repeats: dict[str, int]
    manifestVersion: str
    behaviorDigest: str
    release: str
    evaluatorSetVersion: str
    suiteSha256: str
    evaluators: list[str]
    sessions: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    startedAt: float = 0.0
    finishedAt: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    concurrency: dict[str, int] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    warmup: dict[str, Any] = field(default_factory=dict)     # untimed, unscored (evaluation/warmup.py)
    control: dict[str, Any] = field(default_factory=dict)    # the latency control arm (evaluation/control.py)
    transport: dict[str, Any] = field(default_factory=dict)  # transport re-invokes and the circuit breaker
    schema: str = SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "RunRecord":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in doc.items() if k in names})

    # ---- views for the gate engine
    def eval_results(self) -> list[G.EvalResult]:
        return [G.EvalResult(r["scenarioId"], r["category"], tuple(r.get("tags") or ()), int(r["repeat"]),
                             r["sessionId"], r["evaluatorId"], r.get("value"), r.get("label"), r.get("errorCode"))
                for r in self.results]

    def metrics(self) -> list[G.SessionMetrics]:
        return [G.SessionMetrics(s["sessionId"], float(s["latencyMs"]), int(s["inputTokens"]), int(s["outputTokens"]),
                                 int(s["toolCalls"]), str(s["status"]))
                for s in self.sessions if s.get("status") != "invoke_error"]


def run_key(stage: str, run_id: str) -> str:
    return f"evaluations/{stage}/{run_id}.json"


def baseline_prefix(manifest_version: str, evaluator_set_version: str) -> str:
    return f"baselines/{manifest_version}/{evaluator_set_version}"


def calibration_key(calibration_id: str) -> str:
    return f"calibrations/{calibration_id}/runs.json"


class S3Store:
    """JSON objects in the release bucket, read and written with the caller's own role."""

    def __init__(self, s3: Any, bucket: str) -> None:
        self.s3, self.bucket = s3, bucket

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def put(self, key: str, doc: Mapping[str, Any]) -> str:
        body = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")
        return self.uri(key)

    def get(self, key: str) -> dict | None:
        try:
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        except Exception as exc:
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise

    def keys(self, prefix: str) -> list[str]:
        out, token = [], None
        while True:
            page = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
            out += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return out
            token = page.get("NextContinuationToken")
