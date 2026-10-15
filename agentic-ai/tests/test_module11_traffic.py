# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The traffic driver (module11/demo/traffic.py, 8.1 test_module11_traffic):
pool sizing, the teamConcurrency refusal, 429 retries, the arming deadline, the wholesale start record,
the compare-and-set finish, and the SIGTERM drain."""
import json
import threading
import time

import pytest

from module11.demo import state as S
from module11.demo import traffic as T

RID = "r-0123456789abcdef"
PINS = {"granted": {"deadlineSeconds": 150}}


def ports(tmp_path, send=None, control=None, logs=None, **kw):
    item = control if control is not None else {"state": "WARMING", "rolloutId": RID}
    return T.Ports(send=send or (lambda team, summary, rid: (200, {"status": "completed"})),
                   read_control=item if callable(item) else (lambda: item), candidate_pins=lambda: PINS,
                   state_path=tmp_path / "demo-state.json", log=(logs.append if logs is not None else (lambda line: None)),
                   pid=4242, **kw)


def test_pools_are_round_concurrency_times_mix():
    mix = T.parse_mix("payments=0.6")
    assert mix == {"payments": 0.6, "platform": pytest.approx(0.4)}
    assert T.pool_sizes(20, mix) == {"payments": 12, "platform": 8}
    assert T.split_sessions(120, mix) == {"payments": 72, "platform": 48}
    with pytest.raises(ValueError):
        T.parse_mix("finance=0.5")


def test_refuses_to_start_when_team_concurrency_is_below_the_largest_pool(tmp_path):
    with pytest.raises(T.TrafficError) as err:
        T.Driver(ports(tmp_path), stage="dev", mix=T.parse_mix("payments=0.6"), concurrency=20, team_concurrency=3)
    assert err.value.code == 2 and "teamConcurrency (3)" in str(err.value)
    T.Driver(ports(tmp_path), stage="dev", mix=T.parse_mix("payments=0.6"), concurrency=20, team_concurrency=14)


def test_team_concurrency_429_is_retried_after_2_s_up_to_3_times(tmp_path, monkeypatch):
    calls = {"n": 0}
    sleeps = []

    def send(team, summary, rid):
        calls["n"] += 1
        if calls["n"] <= 2:
            return 429, {"source": "team-concurrency"}
        return 200, {"status": "completed"}
    logs = []
    p = ports(tmp_path, send=send, logs=logs, sleep=sleeps.append)
    code, summary = T.run(p, stage="dev", mix={"payments": 1.0, "platform": 0.0}, concurrency=1, team_concurrency=14,
                          sessions=1, install_signals=False)
    assert code == 0 and summary["retries429"] == 2 and sleeps == [2.0, 2.0]
    assert any("429 retries: 2" in line for line in logs)
    # A fourth 429 is the session's result.
    p = ports(tmp_path, send=lambda *a: (429, {"source": "team-concurrency"}), sleep=lambda s: None)
    code, summary = T.run(p, stage="dev", mix={"payments": 1.0, "platform": 0.0}, concurrency=1, team_concurrency=14,
                          sessions=1, install_signals=False)
    assert summary["retries429"] == 3 and summary["sessions"][0]["httpStatus"] == 429


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_blocks_until_armed_and_says_why_at_the_deadline(tmp_path):
    clock = FakeClock()
    logs = []
    states = iter([{"state": "IDLE", "rolloutId": RID}] * 3 + [{"state": "WARMING", "rolloutId": RID}])
    p = ports(tmp_path, control=lambda: next(states), logs=logs, clock=clock, sleep=clock.sleep)
    code, _ = T.run(p, stage="dev", mix=T.parse_mix("payments=0.6"), concurrency=2, team_concurrency=14, sessions=2,
                    install_signals=False)
    assert code == 0 and clock.t == 1006.0
    clock = FakeClock()
    logs = []
    (tmp_path / "second").mkdir()
    p = ports(tmp_path / "second", control={"state": "WARMING", "rolloutId": "r-ffffffffffffffff"}, logs=logs,
              clock=clock, sleep=clock.sleep)
    code, _ = T.run(p, stage="dev", mix=T.parse_mix("payments=0.6"), concurrency=2, team_concurrency=14, sessions=2,
                    expected_rollout=RID, install_signals=False)
    assert code == 3 and any("not r-0123456789abcdef" in line and "no session sent" in line for line in logs)
    assert S.read(tmp_path / "second" / "demo-state.json").get("traffic") is None


def test_start_record_replaces_an_older_record_wholesale_and_finish_is_written(tmp_path):
    path = tmp_path / "demo-state.json"
    S.update(lambda d: {**d, "armed": {"rolloutId": RID},
                        "traffic": {"rolloutId": "r-old0000000000000", "pid": 1, "startedAt": "2026-10-12T10:00:00+00:00",
                                    "finishedAt": "2026-10-12T10:04:00+00:00", "stopReason": "complete"}}, path)
    seen = {}

    def send(team, summary, rid):
        seen.setdefault("during", S.read(path)["traffic"])
        return 200, {"status": "completed"}
    code, summary = T.run(ports(tmp_path, send=send), stage="dev", mix=T.parse_mix("payments=0.6"), concurrency=2,
                          team_concurrency=14, sessions=4, install_signals=False)
    during = seen["during"]
    assert set(during) == {"rolloutId", "stage", "pid", "startedAt", "deadlineSeconds"}      # no stale finishedAt
    assert during["rolloutId"] == RID and during["pid"] == 4242 and during["deadlineSeconds"] == 150
    doc = S.read(path)
    assert doc["armed"] == {"rolloutId": RID}
    assert doc["traffic"]["stopReason"] == "complete" and doc["traffic"]["finishedAt"] >= during["startedAt"]
    assert code == 0 and summary["sessions"] and len(summary["sessions"]) == 4


def test_a_driver_whose_record_was_replaced_skips_its_finish_write(tmp_path):
    path = tmp_path / "demo-state.json"
    logs = []

    def send(team, summary, rid):
        # Another driver (another rollout) takes the key while this one runs.
        S.update(lambda d: {**d, "traffic": {"rolloutId": "r-1111111111111111", "pid": 99,
                                              "startedAt": S.now_iso()}}, path)
        return 200, {"status": "completed"}
    code, summary = T.run(ports(tmp_path, send=send, logs=logs), stage="dev", mix={"payments": 1.0, "platform": 0.0},
                          concurrency=1, team_concurrency=14, sessions=1, install_signals=False)
    assert code == 0 and summary["recordWritten"] is False
    assert S.read(path)["traffic"] == {"rolloutId": "r-1111111111111111", "pid": 99,
                                       "startedAt": S.read(path)["traffic"]["startedAt"]}
    assert any("traffic record belongs to r-1111111111111111/99; not written" in line for line in logs)


def test_a_failed_start_write_exits_1_without_sessions(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(S, "update", lambda *a, **k: (_ for _ in ()).throw(S.DemoStateError("locked")))
    code, _ = T.run(ports(tmp_path, send=lambda *a: sent.append(a) or (200, {})), stage="dev",
                    mix=T.parse_mix("payments=0.6"), concurrency=2, team_concurrency=14, sessions=2,
                    install_signals=False)
    assert code == 1 and sent == []


def test_sigterm_drains_and_records_stop_reason_signal(tmp_path):
    started = threading.Event()

    def send(team, summary, rid):
        started.set()
        time.sleep(0.3)
        return 200, {"status": "completed"}
    p = ports(tmp_path, send=send)
    driver = T.Driver(p, stage="dev", mix={"payments": 1.0, "platform": 0.0}, concurrency=2, team_concurrency=14)
    driver.rollout_id = driver.wait_armed()
    driver.record_start()
    result = {}
    worker = threading.Thread(target=lambda: result.update(driver.run_fixed(50)))
    worker.start()
    assert started.wait(5)
    driver.signalled.set()
    driver.stop.set()
    worker.join(10)
    rec = S.read(tmp_path / "demo-state.json")["traffic"]
    assert rec["stopReason"] == "signal" and rec["inFlightAtDeadline"] == 0 and "finishedAt" in rec
    assert result["sessionCount"] < 50


def test_a_normal_end_writes_finished_at_with_stop_reason_complete_and_no_in_flight_key(tmp_path):
    code, summary = T.run(ports(tmp_path), stage="dev", mix={"payments": 1.0, "platform": 0.0}, concurrency=1,
                          team_concurrency=14, sessions=2, install_signals=False)
    rec = S.read(tmp_path / "demo-state.json")["traffic"]
    assert code == 0 and summary["stopReason"] == "complete"
    assert rec["stopReason"] == "complete" and rec["finishedAt"] and "inFlightAtDeadline" not in rec


def test_the_drain_waits_at_most_deadline_seconds_plus_30(tmp_path):
    """On SIGTERM the driver waits for in-flight sessions for up to deadlineSeconds + 30 s (180 s in
    the demo), then records how many were still running; the bound is shared by every pool thread."""
    clock = FakeClock()
    joins = []

    class Stuck:
        def join(self, timeout=None):
            joins.append(timeout)
            clock.t += timeout                       # the session never ends: the whole budget is spent

    driver = T.Driver(ports(tmp_path, clock=clock, sleep=clock.sleep), stage="dev",
                      mix={"payments": 1.0, "platform": 0.0}, concurrency=1, team_concurrency=14)
    driver.deadline_seconds = 150
    driver.in_flight = 2
    assert driver._drain([Stuck(), Stuck()]) == 2
    assert joins == [180.0, 0.0] and clock.t == 1180.0


def test_until_terminal_stops_at_rolled_back(tmp_path):
    clock = FakeClock()
    reads = {"n": 0}

    def control():
        reads["n"] += 1
        return {"state": "WARMING" if reads["n"] < 4 else "ROLLED_BACK", "rolloutId": RID, "reason": "tool_selection"}
    logs = []
    p = ports(tmp_path, control=control, logs=logs, clock=clock, sleep=clock.sleep)
    code, summary = T.run(p, stage="dev", mix=T.parse_mix("payments=0.6"), concurrency=4, team_concurrency=14,
                          until_terminal=True, rate=60, max_minutes=5, install_signals=False)
    assert code == 0 and summary["outcome"]["state"] == "ROLLED_BACK"
    assert any("ROLLED_BACK (tool_selection)" in line for line in logs)
    assert json.dumps(summary, default=str)
