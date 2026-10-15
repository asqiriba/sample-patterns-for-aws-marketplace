# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Trajectory matching, the offline twin of the service's
Builtin.Trajectory{ExactOrder,InOrder,AnyOrder}Match evaluators.

The tool sequence of a session is its `execute_tool` spans ordered by span END time
(measured: the service reads nested tools post-order, so a planner's child tool comes before
the parent delegation that called it). Scores are 1.0 or 0.0.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

TOOL_NAME_KEYS = ("gen_ai.tool.name", "tool.name")


def tool_sequence(spans: Iterable[Mapping]) -> list[str]:
    """Tool names of the `execute_tool` spans, sorted by endTimeUnixNano (ties keep input order)."""
    tools = []
    for index, span in enumerate(spans):
        name = str(span.get("name", ""))
        attrs = span.get("attributes") or {}
        if not (name == "execute_tool" or name.startswith("execute_tool ")):
            continue
        tool = next((attrs[k] for k in TOOL_NAME_KEYS if attrs.get(k)), None)
        if tool is None and name.startswith("execute_tool "):
            tool = name[len("execute_tool "):]
        if tool:
            tools.append((int(span.get("endTimeUnixNano") or 0), index, str(tool)))
    return [tool for _, _, tool in sorted(tools)]


def exact_order(expected: Sequence[str], actual: Sequence[str]) -> float:
    """Same tools, same order, nothing extra."""
    return 1.0 if list(expected) == list(actual) else 0.0


def in_order(expected: Sequence[str], actual: Sequence[str]) -> float:
    """Expected tools appear in order; extra tools anywhere are allowed."""
    it = iter(actual)
    return 1.0 if all(any(tool == got for got in it) for tool in expected) else 0.0


def any_order(expected: Sequence[str], actual: Sequence[str]) -> float:
    """Every expected tool appears (with multiplicity); order and extras do not matter."""
    remaining = list(actual)
    for tool in expected:
        if tool not in remaining:
            return 0.0
        remaining.remove(tool)
    return 1.0


MATCHERS = {
    "Builtin.TrajectoryExactOrderMatch": exact_order,
    "Builtin.TrajectoryInOrderMatch": in_order,
    "Builtin.TrajectoryAnyOrderMatch": any_order,
}


def score(evaluator_id: str, expected: Sequence[str], actual: Sequence[str]) -> float:
    try:
        return MATCHERS[evaluator_id](expected, actual)
    except KeyError:
        raise ValueError(f"not a trajectory evaluator: {evaluator_id}") from None
