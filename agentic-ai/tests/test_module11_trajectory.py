# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Trajectory semantics: Exact, InOrder and AnyOrder, extra tools, nested planner tools."""
import pytest

from module11.release import trajectory as T

EXPECTED = ["get_deployment_history", "query_metrics", "search_runbooks", "get_service_logs"]


def test_exact_order():
    assert T.exact_order(EXPECTED, EXPECTED) == 1.0
    assert T.exact_order(EXPECTED, EXPECTED + ["record_incident_note"]) == 0.0
    assert T.exact_order(EXPECTED, list(reversed(EXPECTED))) == 0.0


def test_in_order_allows_extra_tools():
    actual = ["get_deployment_history", "get_blast_radius", "query_metrics", "search_runbooks", "get_service_logs"]
    assert T.in_order(EXPECTED, actual) == 1.0
    friday = ["get_deployment_history", "query_metrics", "get_service_logs"]   # skipped search_runbooks
    assert T.in_order(EXPECTED, friday) == 0.0
    assert T.in_order(EXPECTED, ["query_metrics", "get_deployment_history", "search_runbooks", "get_service_logs"]) == 0.0


def test_any_order_with_multiplicity():
    assert T.any_order(EXPECTED, list(reversed(EXPECTED)) + ["extra"]) == 1.0
    assert T.any_order(["query_metrics", "query_metrics"], ["query_metrics"]) == 0.0


def _span(tool, start, end, name="execute_tool"):
    return {"name": name, "startTimeUnixNano": start, "endTimeUnixNano": end, "attributes": {"gen_ai.tool.name": tool}}


def test_nested_planner_tools_are_ordered_by_span_end():
    """The service reads nested tools post-order (child before the parent delegation)."""
    spans = [
        _span("get_deployment_history", 1, 2),
        _span("delegate_recovery_planning", 3, 10),
        _span("get_blast_radius", 4, 5),
        _span("check_rollback_readiness", 6, 7),
        {"name": "invoke_agent", "endTimeUnixNano": 11, "attributes": {}},
    ]
    assert T.tool_sequence(spans) == ["get_deployment_history", "get_blast_radius", "check_rollback_readiness",
                                      "delegate_recovery_planning"]
    expected = ["get_deployment_history", "get_blast_radius", "check_rollback_readiness", "delegate_recovery_planning"]
    assert T.score("Builtin.TrajectoryExactOrderMatch", expected, T.tool_sequence(spans)) == 1.0


def test_unknown_evaluator():
    with pytest.raises(ValueError):
        T.score("Builtin.Correctness", [], [])
