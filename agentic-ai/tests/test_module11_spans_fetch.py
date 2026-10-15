# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The live half of module11/aws/spans.py against fakes: paginated fetch, the Logs Insights readiness
query and the readiness rule. No AWS."""
import json

import pytest
from botocore.exceptions import ClientError

from module11.aws import spans as S

SID = "m11-eval-0a1b2c3d4e5f-incident-triage-014-1-1b4e2c3d"
GROUP = "/aws/bedrock-agentcore/runtimes/m11_prod_companion-ABC-stable"


def rec(sid=SID, name="invoke_agent devops_companion", **attrs):
    return json.dumps({"scope": {"name": "opentelemetry.instrumentation.devops_companion"}, "name": name,
                       "attributes": {"session.id": sid, **attrs}})


class FakeLogs:
    """FilterLogEvents with an empty first page that still carries a nextToken (the service does this)."""

    def __init__(self, groups):
        self.groups, self.calls = groups, []

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        if kw["logGroupName"] not in self.groups:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "FilterLogEvents")
        matching = [{"message": m} for m in self.groups[kw["logGroupName"]] if kw["filterPattern"].strip('"') in m]
        token = kw.get("nextToken")
        if token is None:
            return {"events": [], "nextToken": "1"}
        if token == "1":  # nosec B105 - a fake pagination token
            return {"events": matching[:1], "nextToken": "2"}
        return {"events": matching[1:]}


def test_session_spans_pages_to_the_end_quotes_the_filter_and_sets_the_window():
    logs = FakeLogs({GROUP: [rec(name="invoke_agent a"), rec(name="execute_tool"), rec(sid=SID + "-x", name="noise")]})
    out = S.session_spans(SID, GROUP, 1000.0, 1100.0, logs=logs)
    assert [r["name"] for r in out] == ["invoke_agent a", "execute_tool"]
    assert len(logs.calls) == 4                          # aws/spans (not found: one call), then the group's three pages
    first = logs.calls[1]
    assert first["filterPattern"] == f'"{SID}"'
    assert first["startTime"] == int((1000 - 60) * 1000) and first["endTime"] == int((1100 + 600) * 1000)


def test_aws_spans_records_come_first_and_a_missing_group_is_skipped():
    logs = FakeLogs({"aws/spans": [rec(name="from_spans")], GROUP: [rec(name="from_runtime")]})
    assert [r["name"] for r in S.session_spans(SID, GROUP, 0, 1, logs=logs)] == ["from_spans", "from_runtime"]
    with pytest.raises(S.SpansError) as exc:
        S.session_spans(SID, GROUP, 0, 1, logs=FakeLogs({GROUP: []}))
    assert exc.value.code == "spans_missing"


def test_other_errors_are_not_swallowed():
    class Denied:
        def filter_log_events(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "FilterLogEvents")
    with pytest.raises(ClientError):
        S.session_spans(SID, GROUP, 0, 1, logs=Denied())


def test_insights_query_is_one_grouped_query_and_rejects_odd_prefixes():
    query = S.insights_query("m11-eval-0a1b2c3d4e5f-")
    assert 'like "m11-eval-0a1b2c3d4e5f-"' in query and "by sid" in query and "limit 10000" in query
    assert "strcontains(@message, \"invoke_agent\")" in query
    with pytest.raises(ValueError):
        S.insights_query('x" | delete')


def test_parse_counts_reads_rows():
    rows = [[{"field": "sid", "value": SID}, {"field": "n", "value": "12"}, {"field": "agents", "value": "2"}],
            [{"field": "n", "value": "1"}]]
    assert S.parse_counts(rows) == {SID: (12, 2)}


class Insights:
    def __init__(self, statuses, rows):
        self.statuses, self.rows, self.started = list(statuses), rows, []

    def start_query(self, **kw):
        self.started.append(kw)
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "results": self.rows if status == "Complete" else []}


def test_readiness_counts_waits_for_complete_and_fails_on_failed():
    logs = Insights(["Running", "Complete"], [[{"field": "sid", "value": SID}, {"field": "n", "value": "3"},
                                              {"field": "agents", "value": "1"}]])
    assert S.readiness_counts(logs, GROUP, "m11-eval-x-", 100, 200, sleep=lambda s: None) == {SID: (3, 1)}
    assert logs.started[0]["logGroupName"] == GROUP and logs.started[0]["limit"] == 10000
    with pytest.raises(RuntimeError):
        S.readiness_counts(Insights(["Failed"], []), GROUP, "m11-eval-x-", 0, 1, sleep=lambda s: None)


def test_tracker_needs_an_invoke_agent_record_and_an_unchanged_count_for_20_seconds():
    t = S.Tracker(["a", "b", "c"])
    assert t.observe({"a": (4, 1), "b": (4, 0)}, 0.0) == []
    assert t.observe({"a": (4, 1), "b": (4, 0)}, 19.0) == []             # 19 s: not yet
    assert t.observe({"a": (4, 1), "b": (4, 0)}, 20.0) == ["a"]          # a: stable 20 s with an invoke_agent record
    assert t.observe({"a": (4, 1), "b": (5, 1)}, 21.0) == []             # b's count changed and it only now has an agent
    assert t.observe({"b": (5, 1)}, 41.0) == ["b"]
    assert t.pending == ["c"] and t.ready == {"a", "b"}


def test_a_changing_count_resets_the_clock():
    t = S.Tracker(["a"])
    t.observe({"a": (3, 1)}, 0.0)
    t.observe({"a": (6, 1)}, 15.0)                                       # more records arrived
    assert t.observe({"a": (6, 1)}, 30.0) == []                          # only 15 s since the last change
    assert t.observe({"a": (6, 1)}, 35.0) == ["a"]


def test_wait_for_spans_schedule_and_give_up():
    now = [1000.0]
    polls = []

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    def poll():
        polls.append(now[0])
        return {"a": (5, 1)}                                            # b never appears
    seen = []
    ready, missing = S.wait_for_spans(poll, ["a", "b"], last_invoke_at=1000.0, on_ready=seen.append, clock=clock,
                                      sleep=sleep)
    assert polls[0] == 1060.0                                           # starts 60 s after the last invocation
    assert polls[1] - polls[0] == 15.0                                  # polls every 15 s
    assert ready == {"a"} and missing == ["b"] and seen == ["a"]
    assert polls[-1] <= 1360.0 and now[0] >= 1360.0                      # gives up 6 minutes after the last invocation


def test_a_failed_poll_is_one_lost_look():
    now = [0.0]
    attempts = []

    def poll():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Timeout")
        return {"a": (2, 1)}
    logged = []
    ready, missing = S.wait_for_spans(poll, ["a"], last_invoke_at=0.0, clock=lambda: now[0],
                                      sleep=lambda s: now.__setitem__(0, now[0] + s), log=logged.append)
    assert ready == {"a"} and missing == [] and logged and "RuntimeError" in logged[0]
