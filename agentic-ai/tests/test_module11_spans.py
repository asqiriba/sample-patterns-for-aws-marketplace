# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pure span selection."""
import json

import pytest

from module11.aws import spans as S

SID = "m11-eval-0a1b2c3d4e5f-incident-triage-014-1-1b4e2c3d"


def rec(sid=SID, scope="opentelemetry.instrumentation.devops_companion", name="execute_tool", **attrs):
    record = {"name": name, "attributes": {"session.id": sid, **attrs}}
    if scope:
        record["scope"] = {"name": scope}
    return json.dumps(record)


def test_keeps_only_exact_session_records_with_scope_and_orders_aws_spans_first():
    raw = [
        {"source": "/aws/bedrock-agentcore/runtimes/m11_prod-x-DEFAULT", "message": rec(name="gen_ai.event")},
        {"source": "aws/spans", "message": rec(name="invoke_agent")},
        {"source": "aws/spans", "message": rec(sid=SID + "-other")},
        {"source": "aws/spans", "message": json.dumps({"scope": {"name": "x"}, "attributes": {"note": SID}})},
        {"source": "aws/spans", "message": json.dumps({"scope": {"name": "x"}, "attributes": {"session": {"id": SID}}})},
        {"source": "aws/spans", "message": rec(scope=None)},
        {"source": "aws/spans", "message": "not json " + SID},
        {"source": "aws/spans", "message": rec(name="execute_tool")},
    ]
    out = S.select_records(raw, SID)
    assert [r["name"] for r in out] == ["invoke_agent", "execute_tool", "gen_ai.event"]


def test_too_many_and_missing():
    raw = [{"source": "aws/spans", "message": rec()}] * (S.MAX_SPANS + 1)
    with pytest.raises(S.SpansError) as exc:
        S.select_records(raw, SID)
    assert exc.value.code == "spans_too_large"
    with pytest.raises(S.SpansError) as exc:
        S.select_records([], SID)
    assert exc.value.code == "spans_missing"


def test_filter_pattern_quotes_the_session_id():
    assert S.filter_pattern(SID) == f'"{SID}"'
    with pytest.raises(ValueError):
        S.filter_pattern('bad"id')
