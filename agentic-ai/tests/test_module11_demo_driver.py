# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The demo driver's process bookkeeping (module11/demo/race.py, demo/run_logged.py) with a fake Popen, a fake
clock and a temporary demo-state.json : killedAt and the ladder-step-2
prompt for a traffic child that exits without finishedAt (also after ROLLED_BACK), none for a clean exit; B6's
revert line only for this take's backgrounded revert, with the four messages, reportedAt and the C6 timing;
run_logged's M11_EXIT line."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from module11.demo import race, run_logged
from module11.demo import state as S
from test_module11_demo_explain import _args, _screen, _timings
from test_module11_demo_smoke import driver
from test_module11_demo_support import RID, RID2, T0, Clock, iso, traffic_record


class FakePopen:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def test_child_running_is_not_reported(tmp_path):
    path = tmp_path / "demo-state.json"
    traffic_record(path)
    assert race.check_traffic_child(FakePopen(None), RID, T0, clock=Clock(T0 + 30), state_path=path) is None


@pytest.mark.parametrize("rolled_back", [False, True])
def test_child_exiting_1_without_finished_at_gets_killed_at_within_one_poll(tmp_path, rolled_back):
    """Also after ROLLED_BACK: 5c's purge would otherwise wait on camera until killedAt + 180 s."""
    path = tmp_path / "demo-state.json"
    traffic_record(path)
    if rolled_back:
        S.update(lambda d: {**d, "note": "ROLLED_BACK landed"}, path)
    clock = Clock(T0 + 75)
    report = race.check_traffic_child(FakePopen(1), RID, T0 - 3, clock=clock, state_path=path)
    assert report is not None and report.code == 1
    assert report.note == "traffic driver exited 1 at t0+1:18"
    assert report.prompt == race.LADDER_2 and "demo rearm" in report.prompt
    assert S.read(path)["traffic"]["killedAt"] == iso(T0 + 75) == report.killed_at
    again = race.check_traffic_child(FakePopen(1), RID, T0 - 3, clock=Clock(T0 + 80), state_path=path)
    assert again is not None and S.read(path)["traffic"]["killedAt"] == iso(T0 + 75)   # written once


def test_child_that_wrote_finished_at_and_exited_0_gets_no_killed_at(tmp_path):
    path = tmp_path / "demo-state.json"
    traffic_record(path, finishedAt=iso(T0 + 140), stopReason="complete", started=T0 - 10)
    assert race.check_traffic_child(FakePopen(0), RID, T0, clock=Clock(T0 + 145), state_path=path) is None
    assert "killedAt" not in S.read(path)["traffic"]


def test_a_record_for_another_rollout_is_never_marked(tmp_path):
    path = tmp_path / "demo-state.json"
    traffic_record(path, rollout_id=RID2)
    report = race.check_traffic_child(FakePopen(1), RID, T0, clock=Clock(T0 + 10), state_path=path)
    assert report is not None and report.killed_at is None and "killedAt" not in S.read(path)["traffic"]


# ---------------------------------------------------------------- B6's revert line
def _log(tmp_path, text):
    path = tmp_path / "drift-revert.log"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("log_text, alive, expected, status", [
    ("retargeting\nM11_EXIT 0 2026-10-20T15:01:00+00:00\n", False, "staging revert: READY on v12", 0),
    ("conflict\nM11_EXIT 3 2026-10-20T15:01:00+00:00\n", False,
     "staging revert failed: exit 3; run module11 drift revert --stage staging after the take", 3),
    ("waiting out UPDATING\n", True, "staging revert still running", "running"),
    ("waiting out UPDATING\n", False, "staging revert failed: exited without status; run module11 drift revert "
                                      "--stage staging after the take", "no status"),
])
def test_b6_maps_the_log_to_the_four_messages_then_sets_reported_at(tmp_path, log_text, alive, expected, status):
    path = tmp_path / "demo-state.json"
    race.write_drift_revert(pid=4242, log=_log(tmp_path, log_text), started_at=iso(T0), backgrounded=False,
                            state_path=path)
    race.set_backgrounded(path)
    line, timing = race.revert_line(state_path=path, alive=lambda pid: alive, read_version=lambda: "v12",
                                    clock=Clock(T0 + 600))
    assert line == expected
    assert timing["cut"] == "C6" and timing["exit"] == status and timing["reportedAt"] == iso(T0 + 600)
    if status in (0, 3):
        assert timing["endedAt"] == "2026-10-20T15:01:00+00:00"
    assert S.read(path)["driftRevert"]["reportedAt"] == iso(T0 + 600)
    # A second d 6 prints nothing.
    assert race.revert_line(state_path=path, alive=lambda pid: alive, clock=Clock(T0 + 700)) == (None, None)


def test_a_foreground_revert_and_a_skipped_2e_print_nothing(tmp_path):
    path = tmp_path / "demo-state.json"
    race.write_drift_revert(pid=1, log=_log(tmp_path, "M11_EXIT 0 x\n"), started_at=iso(T0), backgrounded=False,
                            state_path=path)
    assert race.revert_line(state_path=path, clock=Clock()) == (None, None)
    # A warm-up's backgrounded record replaced by a skipped 2e (M11_CUT=12, s or the staging.lock skip).
    race.set_backgrounded(path)
    race.write_drift_revert(pid=None, log=None, started_at=None, backgrounded=False, state_path=path)
    assert race.revert_line(state_path=path, clock=Clock()) == (None, None)
    assert S.read(path)["driftRevert"] == {"pid": None, "log": None, "startedAt": None, "backgrounded": False,
                                           "reportedAt": None}


def test_retake_waits_up_to_2_minutes_for_a_running_revert_then_keeps_it(tmp_path):
    path = tmp_path / "demo-state.json"
    log = _log(tmp_path, "waiting\n")
    race.write_drift_revert(pid=4242, log=log, started_at=iso(T0), backgrounded=True, state_path=path)
    clock = Clock()
    old = race.retake_check(state_path=path, alive=lambda pid: True, clock=clock, sleep=clock.sleep)
    assert old["pid"] == 4242 and clock.t - T0 >= race.RETAKE_WAIT_S
    clock = Clock()
    finished = {"n": 0}

    def alive(pid):
        finished["n"] += 1
        return finished["n"] < 3
    assert race.retake_check(state_path=path, alive=alive, clock=clock, sleep=clock.sleep) is None
    assert clock.t - T0 < race.RETAKE_WAIT_S


# ---------------------------------------------------------------- run_logged
@pytest.mark.parametrize("code", [0, 3])
def test_run_logged_appends_m11_exit_and_exits_with_the_code(tmp_path, code):
    log = tmp_path / "out.log"
    script = f"import sys; print('working'); sys.exit({code})"
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this interpreter with a fixed test script
    result = subprocess.run([sys.executable, "-m", "module11.demo.run_logged", "--log", str(log), "--",
                             sys.executable, "-c", script], cwd=os.path.dirname(os.path.dirname(__file__)))
    assert result.returncode == code
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "working" and lines[-1].startswith(f"M11_EXIT {code} ")
    assert run_logged.parse_exit(log.read_text(encoding="utf-8"))[0] == code


def test_run_logged_reports_127_for_a_command_that_cannot_start(tmp_path):
    log = tmp_path / "out.log"
    assert run_logged.run(["/nonexistent/m11-command"], log) == 127
    assert run_logged.parse_exit(log.read_text(encoding="utf-8"))[0] == 127


def test_remember_canary_session_stores_the_runtime_session_id(tmp_path, monkeypatch):
    """6a runs `scenario add --from-session <id>`, which accepts only a runtime session ID. The outcome row's `sk`
    ("<epoch ms>#<traffic id>") contains a '#' and is rejected, so the driver must store runtimeSessionId."""
    from types import SimpleNamespace
    from module11.aws import control_table
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "demos"))
    import module11_demo as m
    path = tmp_path / "demo-state.json"
    monkeypatch.setenv("M11_DEMO_STATE", str(path))
    runtime_id = "m11-prod-candidate-v14-00000000-0000-0000-0000-000000000000"
    rows = [
        {"arm": "stable", "team": "payments", "toolsExecuted": ["get_deployment_history"], "sk": "1#stable",
         "runtimeSessionId": "m11-prod-stable-v13-00000000-0000-0000-0000-000000000000"},
        {"arm": "candidate", "team": "payments", "toolsExecuted": ["search_runbooks"], "sk": "2#kept-runbook",
         "runtimeSessionId": "m11-prod-candidate-v14-11111111-1111-1111-1111-111111111111"},
        {"arm": "candidate", "team": "payments", "toolsExecuted": ["get_deployment_history"],
         "sk": "1700000000000#m11-traffic-00000000000000000000", "runtimeSessionId": runtime_id},
    ]
    monkeypatch.setattr(control_table, "outcomes", lambda ddb, table, rid: rows)
    run = SimpleNamespace(stage="prod", ctx=SimpleNamespace(admin=SimpleNamespace(client=lambda name: object())))
    m._remember_canary_session(run, RID)
    canary = S.read(path)["lastCanary"]
    assert canary["sessionId"] == runtime_id and "#" not in canary["sessionId"]
    assert canary["rolloutId"] == RID and canary["stage"] == "prod"


REC_ID = "m11_rec_00000000-AAAAAAAAAA"          # the shape `module11 recommendation run` writes
NO_RECOMMENDATION = "no recommendation recorded yet (the prep chain's recommendation step)"


def _record(directory, rec_id: str, status: str, created: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{rec_id}.json").write_text(
        json.dumps({"id": rec_id, "name": "demo", "createdAt": created, "status": status}), encoding="utf-8")


def test_latest_completed_recommendation_picks_the_newest_completed_record(tmp_path, monkeypatch):
    """`recommendation show` with no id takes the newest record of any status; 6e wants the newest COMPLETED one."""
    m = driver()
    recs = tmp_path / "recommendations"
    _record(recs, "m11_rec_11111111-AAAAAAAAAA", "COMPLETED", "2026-10-09T10:00:00+00:00")
    _record(recs, REC_ID, "COMPLETED", "2026-10-10T10:00:00+00:00")
    _record(recs, "m11_rec_33333333-CCCCCCCCCC", "FAILED", "2026-10-11T10:00:00+00:00")
    _record(recs, "m11_rec_44444444-DDDDDDDDDD", "IN_PROGRESS", "2026-10-12T10:00:00+00:00")
    assert m._latest_completed_recommendation(recs) == REC_ID
    monkeypatch.setattr(m, "LOCAL", tmp_path)                    # no argument: module11/.local/recommendations
    assert m._latest_completed_recommendation() == REC_ID


def test_latest_completed_recommendation_is_none_without_a_usable_record(tmp_path):
    m = driver()
    assert m._latest_completed_recommendation(tmp_path / "missing") is None
    recs = tmp_path / "recommendations"
    recs.mkdir()
    assert m._latest_completed_recommendation(recs) is None
    _record(recs, "m11_rec_33333333-CCCCCCCCCC", "FAILED", "2026-10-11T10:00:00+00:00")
    _record(recs, "m11_rec_44444444-DDDDDDDDDD", "IN_PROGRESS", "2026-10-12T10:00:00+00:00")
    (recs / "corrupt.json").write_text("{not json", encoding="utf-8")
    (recs / "list.json").write_text("[1, 2]", encoding="utf-8")
    assert m._latest_completed_recommendation(recs) is None
    _record(recs, REC_ID, "COMPLETED", "2026-10-10T10:00:00+00:00")        # a good record beside the bad ones
    assert m._latest_completed_recommendation(recs) == REC_ID


def _run_beat6(m, monkeypatch, tmp_path, state: dict | None = None, records=()):
    """Run B6 with no AWS (dev stage, nothing recorded for 6a) and every module11 command stubbed; returns the
    recommendation commands that ran and what B6 printed."""
    state_path = tmp_path / "demo-state.json"
    if state is not None:
        state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("M11_DEMO_STATE", str(state_path))
    monkeypatch.setattr(m, "LOCAL", tmp_path / "local")
    for rec_id, status, created in records:
        _record(tmp_path / "local" / "recommendations", rec_id, status, created)
    ran: list[tuple[str, ...]] = []
    monkeypatch.setattr(m.Run, "cmd", lambda self, *argv, **kw: ran.append(argv) or (0, ""))
    screen = _screen(140)
    m.beat6(m.Run(_args("full", False, canary_stage="dev"), 6, screen, _timings()))
    return [argv for argv in ran if argv[0] == "recommendation"], screen._console.export_text()


def test_beat6_6e_uses_the_latest_completed_recommendation_when_demo_state_has_none(tmp_path, monkeypatch):
    ran, text = _run_beat6(driver(), monkeypatch, tmp_path, records=[
        (REC_ID, "COMPLETED", "2026-10-10T10:00:00+00:00"),
        ("m11_rec_33333333-CCCCCCCCCC", "FAILED", "2026-10-11T10:00:00+00:00")])
    assert ran == [("recommendation", "show", REC_ID), ("recommendation", "import", REC_ID)]
    assert NO_RECOMMENDATION not in text


@pytest.mark.parametrize("recorded", [{"id": "m11_rec_from-state"}, "m11_rec_from-state"])
def test_beat6_6e_prefers_the_recommendation_in_demo_state(tmp_path, monkeypatch, recorded):
    ran, _ = _run_beat6(driver(), monkeypatch, tmp_path, state={"recommendation": recorded},
                        records=[(REC_ID, "COMPLETED", "2026-10-10T10:00:00+00:00")])
    assert ran == [("recommendation", "show", "m11_rec_from-state"), ("recommendation", "import", "m11_rec_from-state")]


def test_beat6_6e_without_any_recommendation_prints_the_same_message_and_runs_nothing(tmp_path, monkeypatch):
    ran, text = _run_beat6(driver(), monkeypatch, tmp_path,
                           records=[("m11_rec_33333333-CCCCCCCCCC", "FAILED", "2026-10-11T10:00:00+00:00")])
    assert ran == []
    assert NO_RECOMMENDATION in text
