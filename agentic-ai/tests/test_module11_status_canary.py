# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 status` during an open canary (cli_live.stage_status / cmd_status): when the only endpoint off the
record is `candidate`, serving exactly the open rollout's candidate version, the stage is CANARY (cyan, exit 0) with
one rollout line read from the control row the way `rollout show` reads it. Every other mismatch stays NOT READY,
red, exit 1, and READY / NOT READY lines keep their exact format. AWS reads are stubbed."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from module11 import cli_live, cli_ops
from module11.aws import endpoints, params

CTX = SimpleNamespace(admin=SimpleNamespace(client=lambda name: object()))
MV = "2026-10-09T17:10:09Z/83ce7d4"
ARMED = {"state": "WARMING", "rolloutId": "r-1", "canaryPct": 50, "candidateVersion": 14, "stableVersion": 13,
         "release": "harness-v28", "endpoints": {"stable": {"version": 13, "release": "harness-v27"},
                                                 "candidate": {"version": 14, "release": "harness-v28"}}}
ROLLOUT_LINE = ("         rollout r-1 WARMING, canary 50%: candidate harness-v28 v14 vs stable harness-v27 v13, "
                "0 sessions so far")


def _live(candidate="14", status="READY", target="14", stable="13"):
    return {"candidate": {"status": status, "liveVersion": candidate, "targetVersion": target},
            "stable": {"status": "READY", "liveVersion": stable}}


@pytest.fixture
def aws(monkeypatch):
    """Stub /current, the endpoints, the control row and the outcome counts; collect what `status` prints."""
    state = {"stage": "prod", "live": _live(), "control": dict(ARMED), "control_error": None, "counts": {},
             "counts_error": None, "calls": [], "said": []}
    current = lambda stage: {"manifestVersion": MV, "deployment": {stage: {  # noqa: E731
        "runtimeId": "rt-1", "release": "harness-v27", "endpoints": {"candidate": 13, "stable": 13}}}}
    monkeypatch.setattr(params, "stage_get",
                        lambda ssm, stage, name: current(stage) if stage == state["stage"] else None)
    monkeypatch.setattr(endpoints, "get", lambda ctl, rid, name: state["live"][name])

    def control(ctx, stage):
        state["calls"].append(stage)
        if state["control_error"]:
            raise state["control_error"]
        return state["control"]

    def counts(ctx, stage, rid):
        if state["counts_error"]:
            raise state["counts_error"]
        return state["counts"]
    monkeypatch.setattr(cli_ops, "_control", control)
    monkeypatch.setattr(cli_ops, "_arm_counts", counts)
    monkeypatch.setattr(cli_live, "_context", lambda root: CTX)
    monkeypatch.setattr(cli_live, "_say", lambda text, style=None: state["said"].append((text, style)))
    return state


def _status(aws) -> int:
    return cli_live.cmd_status(SimpleNamespace(root=Path("."), all=False, stage=aws["stage"]))


def _old_line(stage: str, state: str, eps: str) -> str:
    """The pre-change format, for byte-identical READY / NOT READY lines."""
    return f"{stage:<8} {state:<9} {MV}  harness-v27  {eps}"


def test_armed_prod_is_canary_and_exits_0(aws):
    row = cli_live.stage_status(CTX, "prod")
    assert row["state"] == "CANARY" and row["endpoints"]["candidate"]["canary"] is True
    assert row["rollout"] == {"rolloutId": "r-1", "state": "WARMING", "canaryPct": 50, "candidateVersion": 14,
                              "candidateRelease": "harness-v28", "stableVersion": 13,
                              "stableRelease": "harness-v27", "sessions": 0}
    aws["said"].clear()
    assert _status(aws) == 0
    (line, style), (rollout, rstyle) = aws["said"]
    assert line == _old_line("prod", "CANARY", "candidate v14 (canary), stable v13")
    assert style == rstyle == "cyan"
    assert rollout == ROLLOUT_LINE


def test_sessions_count_both_arms_and_an_unreadable_count_shows_a_question_mark(aws):
    aws["counts"] = {"candidate": {"n": 3}, "stable": {"n": 4}}
    assert cli_live.stage_status(CTX, "prod")["rollout"]["sessions"] == 7
    aws["counts_error"] = RuntimeError("throttled")
    assert _status(aws) == 0
    assert aws["said"][-1][0].endswith(", ? sessions so far")


def test_candidate_on_another_version_stays_not_ready(aws):
    aws["live"] = _live(candidate="15", target=None)
    assert cli_live.stage_status(CTX, "prod")["state"] == "NOT READY"
    assert _status(aws) == 1
    assert aws["said"] == [(_old_line("prod", "NOT READY", "candidate v15 (READY), stable v13"), "red")]


def test_stable_unsettled_too_stays_not_ready_without_reading_the_control_row(aws):
    aws["live"] = _live(stable="12")
    assert _status(aws) == 1
    assert aws["said"][0][1] == "red" and "NOT READY" in aws["said"][0][0] and len(aws["said"]) == 1
    assert aws["calls"] == []


@pytest.mark.parametrize("control", [
    None, {"state": "IDLE"}, {**ARMED, "state": "ROLLED_BACK"}, {**ARMED, "state": "PROMOTED"},
    {**ARMED, "rolloutId": None}, {**ARMED, "candidateVersion": None}, {**ARMED, "candidateVersion": 15}])
def test_no_open_rollout_for_that_candidate_stays_not_ready(aws, control):
    aws["control"] = control
    assert cli_live.stage_status(CTX, "prod")["state"] == "NOT READY"
    assert _status(aws) == 1 and aws["said"][-1][1] == "red"


@pytest.mark.parametrize("state", ["OBSERVING", "ADVANCING"])
def test_every_open_state_counts(aws, state):
    aws["control"] = {**ARMED, "state": state}
    assert cli_live.stage_status(CTX, "prod")["state"] == "CANARY"


def test_candidate_still_updating_stays_not_ready(aws):
    aws["live"] = _live(status="UPDATING")
    assert _status(aws) == 1
    assert aws["said"] == [(_old_line("prod", "NOT READY", "candidate v14 (UPDATING), stable v13"), "red")]


def test_an_unreadable_control_row_stays_not_ready(aws):
    aws["control_error"] = RuntimeError("AccessDenied")
    assert cli_live.stage_status(CTX, "prod")["state"] == "NOT READY"
    assert _status(aws) == 1


def test_ready_line_is_unchanged_and_reads_no_control_row(aws):
    aws["live"] = _live(candidate="13", target=None)
    assert _status(aws) == 0
    assert aws["said"] == [(_old_line("prod", "READY", "candidate v13, stable v13"), "green")]
    assert aws["calls"] == []


def test_the_rule_is_per_stage_dev_armed_is_canary_too(aws):
    aws["stage"] = "dev"
    assert cli_live.stage_status(CTX, "dev")["state"] == "CANARY" and aws["calls"] == ["dev"]
    assert _status(aws) == 0


def test_not_deployed_is_unchanged(aws):
    aws["stage"] = "staging"
    assert cli_live.cmd_status(SimpleNamespace(root=Path("."), all=False, stage="prod")) == 1
    assert aws["said"] == [("prod     NOT DEPLOYED", "yellow")]
