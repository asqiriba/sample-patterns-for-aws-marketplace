# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The rollback controller Lambda: the I/O around `release/controller.py`.

Event: `{command, args, expectedRevision?}` with command in open, tick, shadow-evaluate, shadow-pass,
shadow-fail, abort, reset, endpoints. EventBridge Scheduler sends `{"command": "tick"}` every minute
while a rollout is active; `module11 rollout tick` and `release deploy` invoke it synchronously.
Reserved concurrency 1 serializes invocations, and every write of `{pk: ROLLOUT, sk: CURRENT}` is a
conditional PutItem on `revision`, so this function is the item's only writer in dev, staging and prod.

`AwsPorts` wires the pure controller's ports to DynamoDB (control and outcomes tables), AgentCore
(endpoint retarget and READY poll), Scheduler (GetSchedule -> UpdateSchedule with every field), SSM
(`/current`, `/candidate`, latches), S3 (ledger entries, retried and never blocking) and CloudWatch
(the m11/Controller metrics behind the stuck, stalled and ledger alarms). The shadow verdict:
`start_batch` runs StartBatchEvaluation over one arm's sessions (the endpoint's log group and
service name, the session IDs as the filter, the gated judge evaluators). While it runs, `shadow_obs` adds both
jobs' statuses. Once both completed, it reads each job's result stream and adds the paired score deltas. Only
the `model` profile and the experiment have a shadow phase.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from module11.aws import control_table as CT
from module11.release import controller as C
from module11.release import signals as sig

METRIC_NAMESPACE = "m11/Controller"
ALARM_METRICS = {"m11-rollback-stuck": "RollbackStuck", "m11-rollout-stalled": "RolloutStalled"}
# The shadow verdict's gated judge evaluators (thresholds.yaml statistical gates).
SHADOW_EVALUATORS = ("Builtin.GoalSuccessRate", "Builtin.Correctness")


def client_token(name: str) -> str:
    """A deterministic idempotency token for one batch name: StartBatchEvaluation wants 33-256 characters of
    `[a-zA-Z0-9](-*[a-zA-Z0-9])*`, so underscores become hyphens and a name hash pads it."""
    import hashlib
    return f"{name.replace('_', '-')}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]}"


def result_scores(messages) -> dict[tuple[str, str], float]:
    """Batch-evaluation result events (`gen_ai.evaluation.result` log records) -> {(session ID, evaluator): mean
    score}. A trace-level evaluator scores each trace of a session; the session's score is their mean."""
    sums: dict[tuple[str, str], list[float]] = {}
    for message in messages:
        try:
            doc = json.loads(message)
        except (TypeError, ValueError):
            continue
        attrs = doc.get("attributes") or {}
        sid, ev, value = attrs.get("session.id"), attrs.get("gen_ai.evaluation.name"), attrs.get("gen_ai.evaluation.score.value")
        if doc.get("name") != "gen_ai.evaluation.result" or not sid or not ev or not isinstance(value, (int, float)):
            continue
        sums.setdefault((sid, ev), []).append(float(value))
    return {k: sum(v) / len(v) for k, v in sums.items()}


def shadow_deltas(pairs, candidate: dict, stable: dict) -> dict[str, list[float]]:
    """Per evaluator, candidate minus stable over the pairs both arms scored."""
    out: dict[str, list[float]] = {}
    for ev in SHADOW_EVALUATORS:
        values = [candidate[(c, ev)] - stable[(s, ev)] for c, s in pairs if (c, ev) in candidate and (s, ev) in stable]
        if values:
            out[ev] = values
    return out


def _client(name: str) -> Any:
    from module11 import aws
    return aws.client(name)


class AwsPorts:
    """The controller's ports for one invocation."""

    def __init__(self, env: dict[str, str] | None = None, clients: dict[str, Any] | None = None,
                 clock=time.time, sleeper=time.sleep) -> None:
        self.env = dict(env if env is not None else os.environ)
        self.stage = self.env["M11_STAGE"]
        self._clients = dict(clients or {})
        self._clock, self._sleep = clock, sleeper

    def c(self, name: str) -> Any:
        if name not in self._clients:
            self._clients[name] = _client(name)
        return self._clients[name]

    # -- time
    def now(self) -> float:
        return float(self._clock())

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    # -- the control item and its audit trail
    def get_item(self) -> dict | None:
        return CT.get(self.c("dynamodb"), self.env["CONTROL_TABLE"])

    def put_item(self, item: dict, expected_revision: int | None) -> None:
        try:
            CT.put_conditional(self.c("dynamodb"), self.env["CONTROL_TABLE"], item, expected_revision)
        except CT.ConcurrentUpdate as exc:
            raise C.ConditionalCheckFailed(str(exc)) from exc

    def put_audit(self, event: dict) -> None:
        CT.put_event(self.c("dynamodb"), self.env["CONTROL_TABLE"], event)

    # -- signals
    def log_for(self, item: dict) -> sig.SessionLog:
        rows = CT.outcomes(self.c("dynamodb"), self.env["OUTCOMES_TABLE"], item["rolloutId"])
        sessions = [sig.session_from_outcome(r) for r in rows if not r.get("shadow")]
        return sig.SessionLog(sessions, [float(s) for s in item["stepStarts"]])

    def shadow_obs(self, item: dict) -> dict:
        """Complete (candidate shadow session, original stable session) pairs from the outcomes table; while a
        verdict runs, both batch evaluations' statuses, and once both completed, the paired score deltas."""
        rows = CT.outcomes(self.c("dynamodb"), self.env["OUTCOMES_TABLE"], item["rolloutId"])
        originals = {str(r["sk"]).split("#", 1)[-1]: r["runtimeSessionId"] for r in rows
                     if not r.get("shadow") and r.get("arm") == "stable"}
        pairs = [(r["runtimeSessionId"], originals[r["originalRequestId"]]) for r in rows
                 if r.get("shadow") and r.get("originalRequestId") in originals]
        obs: dict[str, Any] = {"shadowPairs": pairs}
        shadow = item.get("shadow") or {}
        ids = shadow.get("batchIds") or {}
        if shadow.get("running") and ids.get("c") and ids.get("s"):
            core = self.c("bedrock-agentcore")
            jobs = {arm: core.get_batch_evaluation(batchEvaluationId=ids[arm]) for arm in ("c", "s")}
            obs["batches"] = {arm: {"status": job.get("status")} for arm, job in jobs.items()}
            if all(job.get("status") in ("COMPLETED", "COMPLETED_WITH_ERRORS") for job in jobs.values()):
                scores = {arm: self.batch_scores(job) for arm, job in jobs.items()}
                obs["shadowDeltas"] = shadow_deltas(pairs, scores["c"], scores["s"])
        return obs

    def batch_scores(self, job: dict) -> dict[tuple[str, str], float]:
        """{(session ID, evaluator): mean score} from a batch evaluation's result stream."""
        out = ((job.get("outputConfig") or {}).get("cloudWatchConfig") or {})
        group, stream = out.get("logGroupName"), out.get("logStreamName")
        if not group or not stream:
            return {}
        logs, token, events = self.c("logs"), None, []
        while True:
            page = logs.get_log_events(logGroupName=group, logStreamName=stream, startFromHead=True,
                                       **({"nextToken": token} if token else {}))
            events += page.get("events") or []
            if not page.get("events") or page.get("nextForwardToken") in (None, token):
                break
            token = page["nextForwardToken"]
        return result_scores(e.get("message") for e in events)

    def start_batch(self, name: str, session_ids: list[str]) -> str:
        """One StartBatchEvaluation over one arm's sessions (the shadow verdict): the gated judge
        evaluators, the arm endpoint's log group and service name, the session IDs as the filter."""
        arm = "candidate" if name.rsplit("_", 1)[-1].startswith("c") else "stable"
        runtime_id = self.env["RUNTIME_ID"]
        job = self.c("bedrock-agentcore").start_batch_evaluation(
            batchEvaluationName=name, clientToken=client_token(name),
            evaluators=[{"evaluatorId": e} for e in SHADOW_EVALUATORS],
            dataSourceConfig={"cloudWatchLogs": {
                "serviceNames": [f"{runtime_id.rsplit('-', 1)[0]}.{arm}"],
                "logGroupNames": [f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{arm}"],
                "filterConfig": {"sessionIds": list(session_ids)}}},
            tags={"Project": "m11-devops-companion", "Stage": self.stage},
            description=f"m11 shadow verdict, {arm} arm ({len(session_ids)} sessions)")
        return job["batchEvaluationId"]

    # -- endpoints
    def get_endpoint(self, qualifier: str) -> dict:
        ep = self.c("bedrock-agentcore-control").get_agent_runtime_endpoint(
            agentRuntimeId=self.env["RUNTIME_ID"], endpointName=qualifier)
        return {"status": ep.get("status"), "liveVersion": ep.get("liveVersion"), "targetVersion": ep.get("targetVersion")}

    def update_endpoint(self, qualifier: str, version: int) -> None:
        self.c("bedrock-agentcore-control").update_agent_runtime_endpoint(
            agentRuntimeId=self.env["RUNTIME_ID"], endpointName=qualifier, agentRuntimeVersion=str(int(version)))

    # -- schedule
    def get_schedule(self) -> dict:
        return self.c("scheduler").get_schedule(Name=self.env["SCHEDULE_NAME"],
                                                GroupName=self.env.get("SCHEDULE_GROUP", "default"))

    def update_schedule(self, **fields: Any) -> None:
        self.c("scheduler").update_schedule(**fields)

    # -- parameters, ledger, metrics, events
    def get_param(self, name: str) -> str | None:
        try:
            return self.c("ssm").get_parameter(Name=name)["Parameter"]["Value"]
        except Exception as exc:  # noqa: BLE001
            if str(getattr(exc, "response", {}).get("Error", {}).get("Code", "")) == "ParameterNotFound":
                return None
            raise

    def put_param(self, name: str, value: str) -> None:
        advanced = name in (self.env.get("CURRENT_PARAM"), self.env.get("CANDIDATE_PARAM"))
        self.c("ssm").put_parameter(Name=name, Value=value, Type="String", Overwrite=True,
                                    Tier="Advanced" if advanced else "Standard")

    def put_ledger(self, key: str, body: bytes) -> None:
        self.c("s3").put_object(Bucket=self.env["RELEASE_BUCKET"], Key=key, Body=body, ContentType="application/json")

    def put_metric(self, namespace: str, name: str, value: float) -> None:
        # Every controller metric lives under m11/Controller with a Stage dimension (the IAM condition's namespace).
        self.c("cloudwatch").put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[{
            "MetricName": name, "Value": float(value), "Unit": "Count",
            "Dimensions": [{"Name": "Stage", "Value": self.stage}]}])

    def emit(self, name: str, detail: dict) -> None:
        # The m11-releases bus arrives with the pipeline; until then the event is logged.
        self.log("INFO", "event", name=name, detail=detail)

    def alarm(self, name: str, reason: str | None = None) -> None:
        self.log("WARN", "alarm", alarm=name, reason=reason)
        metric = ALARM_METRICS.get(name)
        if metric:
            self.put_metric(METRIC_NAMESPACE, metric, 1.0)

    def log(self, level: str, event: str, **fields: Any) -> None:
        print(json.dumps({"level": level, "event": event, "stage": self.stage, **fields}, default=str), flush=True)


def controller(ports: AwsPorts) -> C.Controller:
    return C.Controller(ports, ports.stage, ports.env["CURRENT_PARAM"], ports.env["CANDIDATE_PARAM"])


def handler(event: dict, context: Any = None) -> dict:
    ports = AwsPorts()
    if not isinstance(event, dict) or event.get("command") not in C.COMMANDS:
        return {"ok": False, "error": "invalid_command", "detail": f"command must be one of {list(C.COMMANDS)}"}
    result = controller(ports).handle(event)
    ports.log("INFO", "command", command=event.get("command"), result=result)
    return result
