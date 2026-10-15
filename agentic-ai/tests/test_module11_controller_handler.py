# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The controller and shadow-worker Lambdas' I/O (lambdas/*/handler.py, aws/control_table.py, aws/rollouts.py)
against in-memory fakes: conditional writes on revision with Decimal numbers, the schedule copy, the outcomes
query behind the signals, throttle retries that serialize concurrent ticks, and the shadow work item."""
import io
import json
import threading
from decimal import Decimal

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from module11.aws import control_table as CT
from module11.aws import rollouts
from module11.lambdas.controller import handler as H
from module11.lambdas.shadow_worker import handler as W
from module11.release import controller as C

SER, DE = TypeSerializer(), TypeDeserializer()
RID = "r-00112233445566aa"


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeDynamo:
    def __init__(self):
        self.tables: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.before_put = None

    def _key(self, item):
        return (item["pk"]["S"], item["sk"]["S"])

    def get_item(self, TableName, Key, ConsistentRead=False):
        row = self.tables.get(TableName, {}).get(self._key(Key))
        return {"Item": row} if row else {}

    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook()
        with self.lock:
            table = self.tables.setdefault(TableName, {})
            existing = table.get(self._key(Item))
            if ConditionExpression == "attribute_not_exists(pk)" and existing:
                raise ClientError("ConditionalCheckFailedException")
            if ConditionExpression == "revision = :r":
                if not existing or existing["revision"]["N"] != ExpressionAttributeValues[":r"]["N"]:
                    raise ClientError("ConditionalCheckFailedException")
            table[self._key(Item)] = Item

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, **kw):
        pk = ExpressionAttributeValues[":pk"]["S"]
        rows = [v for (p, s), v in sorted(self.tables.get(TableName, {}).items()) if p == pk]
        if ":e" in ExpressionAttributeValues:
            rows = [r for r in rows if r["sk"]["S"].startswith(ExpressionAttributeValues[":e"]["S"])]
        return {"Items": rows}

    def plain(self, table, pk="ROLLOUT", sk="CURRENT"):
        row = self.tables[table][(pk, sk)]
        return {k: DE.deserialize(v) for k, v in row.items()}


class FakeScheduler:
    def __init__(self):
        self.state = "DISABLED"
        self.updates = []

    def get_schedule(self, Name, GroupName):
        return {"Name": Name, "GroupName": GroupName, "ScheduleExpression": "cron(* * * * ? *)", "State": self.state,
                "FlexibleTimeWindow": {"Mode": "OFF"}, "Arn": "arn:x", "CreationDate": "now",
                "Target": {"Arn": "arn:aws:lambda:us-east-1:111122223333:function:m11-dev-controller:3",
                           "RoleArn": "arn:aws:iam::111122223333:role/m11-dev-scheduler", "Input": '{"command":"tick"}',
                           "RetryPolicy": {"MaximumEventAgeInSeconds": 60, "MaximumRetryAttempts": 0}}}

    def update_schedule(self, **fields):
        assert "Arn" not in fields and "CreationDate" not in fields and fields["Target"]["RoleArn"]
        self.updates.append(fields)
        self.state = fields["State"]


class FakeControl:
    def __init__(self):
        self.live = {"stable": 7, "candidate": 7}
        self.updates = []

    def get_agent_runtime_endpoint(self, agentRuntimeId, endpointName):
        return {"status": "READY", "liveVersion": str(self.live[endpointName])}

    def update_agent_runtime_endpoint(self, agentRuntimeId, endpointName, agentRuntimeVersion):
        self.updates.append((endpointName, agentRuntimeVersion))
        self.live[endpointName] = int(agentRuntimeVersion)


class FakeSSM:
    def __init__(self):
        self.values = {"/m11/devops-companion/dev/candidate": json.dumps(
            {"manifestVersion": "mv8", "deployment": {"dev": {"endpoints": {"stable": "7", "candidate": "8"}}}})}
        self.puts = []

    def get_parameter(self, Name):
        if Name not in self.values:
            raise ClientError("ParameterNotFound")
        return {"Parameter": {"Value": self.values[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite, Tier):
        self.puts.append((Name, Tier))
        self.values[Name] = Value


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda **kw: self.calls.append((name, kw)) or {}


ENV = {"M11_STAGE": "dev", "CONTROL_TABLE": "m11-dev-control", "OUTCOMES_TABLE": "m11-dev-outcomes",
       "RUNTIME_ID": "m11_dev_companion-X", "SCHEDULE_NAME": "m11-dev-controller", "SCHEDULE_GROUP": "default",
       "CURRENT_PARAM": "/m11/devops-companion/dev/current", "CANDIDATE_PARAM": "/m11/devops-companion/dev/candidate",
       "RELEASE_BUCKET": "m11-release-111122223333-us-east-1"}


class Clock:
    def __init__(self, t=1792508400.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_ports(ddb=None, clock=None, control=None):
    clock = clock or Clock()
    clients = {"dynamodb": ddb or FakeDynamo(), "scheduler": FakeScheduler(), "bedrock-agentcore-control":
               control or FakeControl(), "ssm": FakeSSM(), "s3": Recorder(), "cloudwatch": Recorder()}
    return H.AwsPorts(env=ENV, clients=clients, clock=clock, sleeper=clock.sleep), clients, clock


def seed_idle(ddb):
    CT.put_conditional(ddb, ENV["CONTROL_TABLE"], {"state": "IDLE", "revision": 4, "endpoints": {
        "stable": {"version": "7", "manifestVersion": "mv7", "behaviorDigest": "sha256:" + "7" * 64, "release": "operator-7"},
        "candidate": {"version": "7", "manifestVersion": "mv7", "behaviorDigest": "sha256:" + "7" * 64,
                      "release": "operator-7"}}}, None)


OPEN = {"rolloutId": RID, "profile": "demo", "stableVersion": 7, "candidateVersion": 8,
        "behaviorDigest": "sha256:" + "8" * 64, "manifestVersion": "mv8", "release": "operator-8"}


def test_open_writes_decimal_numbers_conditionally_and_enables_the_schedule():
    ports, clients, clock = make_ports()
    seed_idle(clients["dynamodb"])
    result = H.controller(ports).handle({"command": "open", "args": OPEN})
    assert result == {"ok": True, "state": "WARMING", "revision": 5, "note": ""}
    raw = CT.get_raw(clients["dynamodb"], ENV["CONTROL_TABLE"])
    assert isinstance(raw["openedAt"], Decimal) and raw["openedAt"] == Decimal(repr(clock.t))
    assert CT.get(clients["dynamodb"], ENV["CONTROL_TABLE"])["endpoints"]["candidate"]["version"] == 8
    assert clients["scheduler"].state == "ENABLED"
    events = CT.events(clients["dynamodb"], ENV["CONTROL_TABLE"], RID)
    assert [e["decision"] for e in events] == ["IDLE->WARMING"]


def test_a_conflicting_write_is_retried_once_then_reported():
    ports, clients, _ = make_ports()
    ddb = clients["dynamodb"]
    seed_idle(ddb)

    def interloper():   # someone else bumps the revision between the read and the conditional put
        row = ddb.tables[ENV["CONTROL_TABLE"]][("ROLLOUT", "CURRENT")]
        row["revision"] = {"N": str(int(row["revision"]["N"]) + 1)}
    ddb.before_put = interloper
    result = H.controller(ports).handle({"command": "open", "args": OPEN})
    assert result["ok"] and result["revision"] == 6           # re-read revision 5, wrote 6
    with pytest.raises(C.ConditionalCheckFailed):
        ports.put_item({"state": "IDLE", "revision": 9}, 2)


def outcome(arm, ms, runbooks, team="payments"):
    tools = ["get_deployment_history", "query_metrics"] + (["search_runbooks"] if runbooks else []) + ["get_service_logs"]
    return {"pk": f"ROLLOUT#{RID}", "sk": f"{ms}#req-{arm}-{ms}", "arm": arm, "status": "completed", "team": team,
            "toolsOffered": ["get_deployment_history", "query_metrics", "search_runbooks", "get_service_logs"],
            "toolsExecuted": tools, "toolFailures": [], "latencyMs": 25000, "inputTokens": 6000, "outputTokens": 300,
            "cachedInputTokens": 0, "shadow": False}


def test_ticks_read_the_outcomes_table_and_a_tool_selection_shift_breaches():
    ports, clients, clock = make_ports()
    ddb = clients["dynamodb"]
    seed_idle(ddb)
    ctl = H.controller(ports)
    ctl.handle({"command": "open", "args": OPEN})
    start_ms = int(clock.t * 1000)
    for i in range(12):
        for arm, runbooks in (("candidate", i % 3 == 0), ("stable", True)):
            CT.put_event(ddb, ENV["OUTCOMES_TABLE"], outcome(arm, start_ms + 1000 + i * 5000 + (arm == "stable"), runbooks))
    clock.t += 70
    first = ctl.handle({"command": "tick"})
    assert first["state"] == "OBSERVING"
    item = CT.get(ddb, ENV["CONTROL_TABLE"])
    assert item["breaches"] == 1 and item["lastSignals"]["tool_selection"] == "breach"
    for i in range(6):
        for arm, runbooks in (("candidate", False), ("stable", True)):
            CT.put_event(ddb, ENV["OUTCOMES_TABLE"], outcome(arm, start_ms + 71000 + i * 3000 + (arm == "stable"), runbooks))
    clock.t += 60
    second = ctl.handle({"command": "tick"})
    assert second["state"] == "ROLLED_BACK"                    # READY within the same invocation (fake: at once)
    assert clients["bedrock-agentcore-control"].updates == [("candidate", "7")]
    assert clients["scheduler"].state == "DISABLED"
    names = [n for n, _ in clients["ssm"].puts]
    assert f"/m11/devops-companion/latch/{'8' * 64}" in names and ENV["CANDIDATE_PARAM"] in names
    assert ("/m11/devops-companion/dev/candidate", "Advanced") in clients["ssm"].puts
    raw = CT.get_raw(ddb, ENV["CONTROL_TABLE"])
    assert isinstance(raw["rolledBackAt"], Decimal) and raw["endpoints"]["candidate"]["version"] == "7"
    assert any(name == "put_object" for name, _ in clients["s3"].calls)      # the latch ledger entry


def test_alarms_become_m11_controller_metrics_with_a_stage_dimension():
    ports, clients, _ = make_ports()
    ports.alarm("m11-rollback-stuck")
    ports.put_metric("m11", "LedgerWriteFailures", 1.0)
    data = [kw for name, kw in clients["cloudwatch"].calls if name == "put_metric_data"]
    assert [d["Namespace"] for d in data] == ["m11/Controller", "m11/Controller"]
    assert [d["MetricData"][0]["MetricName"] for d in data] == ["RollbackStuck", "LedgerWriteFailures"]
    assert data[0]["MetricData"][0]["Dimensions"] == [{"Name": "Stage", "Value": "dev"}]


def test_the_handler_rejects_unknown_commands(monkeypatch):
    monkeypatch.setattr(H, "AwsPorts", lambda: type("P", (), {"stage": "dev", "env": ENV,
                                                              "log": lambda *a, **k: None})())
    assert H.handler({"command": "promote"})["error"] == "invalid_command"


class FakeLambda:
    def __init__(self, throttles=0, result=None):
        self.throttles, self.calls = throttles, []
        self.result = result or {"ok": True, "state": "OBSERVING", "revision": 9}

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append((FunctionName, json.loads(Payload)))
        if self.throttles:
            self.throttles -= 1
            raise ClientError("TooManyRequestsException")
        return {"Payload": io.BytesIO(json.dumps(self.result).encode())}


def test_concurrent_ticks_serialize_through_throttle_retries():
    lam = FakeLambda(throttles=2)
    sleeps = []
    result = rollouts.invoke(lam, "dev", "tick", sleep=sleeps.append)
    assert result["ok"] and result["throttles"] == 2 and sleeps == [2.0, 2.0]
    assert lam.calls[0] == ("m11-dev-controller", {"command": "tick", "args": {}})
    with pytest.raises(rollouts.ControllerError):
        rollouts.invoke(FakeLambda(result={"ok": False, "error": "invalid_transition", "detail": "x"}), "dev", "abort")


def test_shadow_worker_invokes_the_candidate_with_side_effects_suppressed():
    ddb = FakeDynamo()
    CT.put_conditional(ddb, "m11-prod-control", {"state": "OBSERVING", "rolloutId": RID, "revision": 3,
                                                 "endpoints": {"candidate": {"version": "9"}}}, None)

    class Core:
        def __init__(self):
            self.calls = []

        def invoke_agent_runtime(self, **kw):
            self.calls.append(("invoke", kw))
            body = 'event: result\ndata: {"status": "completed"}\n\n'
            return {"response": io.BytesIO(body.encode())}

        def stop_runtime_session(self, **kw):
            self.calls.append(("stop", kw))
    core = Core()
    env = {"CONTROL_TABLE": "m11-prod-control", "OUTCOMES_TABLE": "m11-prod-outcomes", "SESSION_STAGE": "prod",
           "RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/m11_prod_companion-X"}
    message = {"conversationId": "conv-12345678", "change_summary": "payments-service is slow.", "team": "payments",
               "principal": "u-1", "originalRequestId": "req-1", "rolloutId": RID,
               "candidate": {"manifestVersion": "2026-10-20T14:02:11Z/a7f3c91", "behaviorDigest": "sha256:" + "a" * 64,
                             "release": "harness-v8"}}
    assert W.process(message, env=env, clients={"dynamodb": ddb, "bedrock-agentcore": core}) == "completed"
    invoke = core.calls[0][1]
    payload = json.loads(invoke["payload"])
    assert invoke["qualifier"] == "candidate" and invoke["runtimeSessionId"].startswith("m11-prod-shadow-v9-")
    assert payload["context"]["variant"] == "shadow" and payload["context"]["shadow"] is True
    assert "traceparent" not in payload["context"] and core.calls[1][0] == "stop"
    row = next(iter(ddb.tables["m11-prod-outcomes"].values()))
    plain = {k: DE.deserialize(v) for k, v in row.items()}
    assert plain["shadow"] is True and plain["originalRequestId"] == "req-1" and plain["pk"] == f"ROLLOUT#{RID}"
    # The rollout moved on: the work item is dropped without an invocation.
    CT.put_conditional(ddb, "m11-prod-control", {"state": "ROLLED_BACK", "rolloutId": RID, "revision": 4}, 3)
    assert W.process(message, env=env, clients={"dynamodb": ddb, "bedrock-agentcore": core}) == "skipped"
    assert len(core.calls) == 2


# ---------------------------------------------------------------- shadow verdict I/O
class FakeBatch:
    def __init__(self, status="COMPLETED"):
        self.started, self.status = [], status

    def start_batch_evaluation(self, **kw):
        self.started.append(kw)
        return {"batchEvaluationId": kw["batchEvaluationName"] + "-abc"}

    def get_batch_evaluation(self, batchEvaluationId):
        return {"batchEvaluationId": batchEvaluationId, "status": self.status, "outputConfig": {"cloudWatchConfig": {
            "logGroupName": "/aws/bedrock-agentcore/evaluations/batch-evaluations/results/default",
            "logStreamName": f"run-{batchEvaluationId}"}}}


def _result(sid, ev, value):
    return json.dumps({"name": "gen_ai.evaluation.result", "attributes": {
        "session.id": sid, "gen_ai.evaluation.name": ev, "gen_ai.evaluation.score.value": value}})


class FakeLogs:
    def __init__(self, streams):
        self.streams = streams

    def get_log_events(self, logGroupName, logStreamName, startFromHead, nextToken=None):
        events = self.streams.get(logStreamName, [])
        if nextToken == "end":
            return {"events": [], "nextForwardToken": "end"}
        return {"events": [{"message": m} for m in events], "nextForwardToken": "end"}


def test_start_batch_names_the_arm_endpoint_and_the_sessions():
    ports, clients, _ = make_ports()
    ports._clients["bedrock-agentcore"] = FakeBatch()
    bid = ports.start_batch(C.batch_name("dev", RID, "c", 1), ["s1", "s2"])
    (kw,) = ports._clients["bedrock-agentcore"].started
    assert bid == "m11_dev_shadow_00112233445566aa_c1-abc"
    assert kw["clientToken"].startswith("m11-dev-shadow-00112233445566aa-c1-")
    import re
    for name in (kw["batchEvaluationName"], "m11_dev_shadow_x_c1"):          # the API's pattern and length
        token = H.client_token(name)
        assert re.fullmatch(r"[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}", token) and 33 <= len(token) <= 256
    assert H.client_token("a_b") == H.client_token("a_b")
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", kw["batchEvaluationName"])
    logs = kw["dataSourceConfig"]["cloudWatchLogs"]
    assert logs["serviceNames"] == ["m11_dev_companion.candidate"]
    assert logs["logGroupNames"] == ["/aws/bedrock-agentcore/runtimes/m11_dev_companion-X-candidate"]
    assert logs["filterConfig"] == {"sessionIds": ["s1", "s2"]}
    assert [e["evaluatorId"] for e in kw["evaluators"]] == list(H.SHADOW_EVALUATORS)
    assert kw["tags"] == {"Project": "m11-devops-companion", "Stage": "dev"}
    ports.start_batch(C.batch_name("dev", RID, "s", 1), ["o1"])
    assert ports._clients["bedrock-agentcore"].started[1]["dataSourceConfig"]["cloudWatchLogs"]["serviceNames"] == \
        ["m11_dev_companion.stable"]


def test_result_scores_average_traces_and_ignore_other_records():
    rows = [_result("a", "Builtin.Correctness", 1.0), _result("a", "Builtin.Correctness", 0.0),
            _result("a", "Builtin.GoalSuccessRate", 1.0), json.dumps({"name": "other"}), "not json"]
    assert H.result_scores(rows) == {("a", "Builtin.Correctness"): 0.5, ("a", "Builtin.GoalSuccessRate"): 1.0}


def test_shadow_obs_adds_statuses_then_paired_deltas():
    ddb = FakeDynamo()
    for sk, row in ((f"1#req-1", {"arm": "stable", "runtimeSessionId": "s-orig"}),
                    (f"2#shadow-req-1", {"arm": "candidate", "shadow": True, "runtimeSessionId": "c-shadow",
                                         "originalRequestId": "req-1"})):
        ddb.put_item(TableName=ENV["OUTCOMES_TABLE"], Item={k: SER.serialize(v) for k, v in
                                                            {"pk": f"ROLLOUT#{RID}", "sk": sk, **row}.items()})
    ports, clients, _ = make_ports(ddb=ddb)
    item = {"rolloutId": RID, "shadow": {"running": True, "batchIds": {"c": "bc", "s": "bs"}}}
    ports._clients["bedrock-agentcore"] = FakeBatch(status="IN_PROGRESS")
    obs = ports.shadow_obs(item)
    assert obs["shadowPairs"] == [("c-shadow", "s-orig")] and obs["batches"] == {"c": {"status": "IN_PROGRESS"},
                                                                                 "s": {"status": "IN_PROGRESS"}}
    assert "shadowDeltas" not in obs
    ports._clients["bedrock-agentcore"] = FakeBatch(status="COMPLETED")
    ports._clients["logs"] = FakeLogs({"run-bc": [_result("c-shadow", "Builtin.GoalSuccessRate", 0.0),
                                           _result("c-shadow", "Builtin.Correctness", 1.0)],
                                "run-bs": [_result("s-orig", "Builtin.GoalSuccessRate", 1.0),
                                           _result("s-orig", "Builtin.Correctness", 1.0)]})
    obs = ports.shadow_obs(item)
    assert obs["shadowDeltas"] == {"Builtin.GoalSuccessRate": [-1.0], "Builtin.Correctness": [0.0]}
    assert ports.shadow_obs({"rolloutId": RID, "shadow": {}}) == {"shadowPairs": [("c-shadow", "s-orig")]}
