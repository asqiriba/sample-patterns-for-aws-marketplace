# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`memory purge` under the purge-wait rule (module11/aws/memory.py: purge_wait, purge).

The fake control item comes from `controller.decide()` output (epoch-second floats), converted to
Decimal the way DynamoDB returns numbers; demo-state.json and the Memory client are fakes."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from module11.aws import memory
from module11.release import controller as ctl
from module11.release import pins as P

RID = "r-00112233445566aa"
OPENED = 1792508400.0
PINS = {"granted": {"deadlineSeconds": 150}}


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def ddb(item):
    """What DynamoDB hands back: every number a Decimal."""
    return json.loads(json.dumps(item, default=str), parse_float=Decimal, parse_int=Decimal)


def rolled_back_item(at):
    args = {"rolloutId": RID, "profile": "demo", "stableVersion": 7, "candidateVersion": 8,
            "behaviorDigest": "sha256:" + "ab" * 32, "manifestVersion": "2026-10-20T14:00:00Z/abcdef1",
            "release": "harness-v8"}
    item = ctl.decide({"state": "IDLE", "revision": 3, "endpoints": {"stable": {"version": 7}}}, "open", args,
                      None, None, OPENED).item
    item = ctl.decide(item, "abort", {"reason": "tool_selection"}, None, None, at - 30).item
    ready = {"endpoints": {"candidate": {"status": "READY", "liveVersion": 7}}}
    item = ctl.decide(item, "tick", {}, None, None, at, ready).item
    assert item["state"] == "ROLLED_BACK" and isinstance(item["rolledBackAt"], float)
    return ddb(item)


class Clock:
    def __init__(self, t):
        self.t = float(t)
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeMemory:
    def __init__(self, ids):
        self.ids = list(ids)
        self.deleted = []

    def list_memory_records(self, **kw):
        assert kw["namespacePath"] == "/devops-companion/dev/release/harness-v8/"
        return {"memoryRecordSummaries": [{"memoryRecordId": i, "namespaces": [kw["namespacePath"]]} for i in self.ids]}

    def batch_delete_memory_records(self, memoryId, records):
        self.deleted += [r["memoryRecordId"] for r in records]
        return {"successfulRecords": [{"memoryRecordId": r["memoryRecordId"]} for r in records], "failedRecords": []}


def run(clock, rollout_reads, driver, *, dry_run=False, mem=None):
    """The `memory purge --release` flow: purge_wait, then purge; returns (exit code, memory, message)."""
    mem = mem or FakeMemory(["m-1", "m-2"])
    reads = iter(rollout_reads) if isinstance(rollout_reads, list) else None

    def read_rollout():
        return next(reads) if reads else rollout_reads()

    def read_driver():
        if isinstance(driver, Exception):
            raise driver
        return driver() if callable(driver) else driver

    try:
        if not dry_run:
            memory.purge_wait(read_rollout, read_driver, PINS, clock=clock, sleep=clock.sleep)
        report = memory.purge("mem-1", stage="dev", release="harness-v8", dry_run=dry_run, data_plane=mem)
        return 0, mem, report
    except (memory.PurgeNotReady, P.PinsViolation) as exc:
        return 5, mem, str(exc)


def test_killed_at_waits_past_the_120_s_mark_then_deletes():
    at = OPENED + 200
    item = rolled_back_item(at)
    clock = Clock(at + 10)
    killed = at + 20                         # ready at killedAt + 180 = 190 s after the purge started
    driver = {"rolloutId": RID, "pid": 7, "startedAt": iso(OPENED + 5), "killedAt": iso(killed)}
    code, mem, report = run(clock, lambda: item, driver)
    assert code == 0 and mem.deleted == ["m-1", "m-2"] and report.deleted == 2
    assert clock.t >= killed + 180 and clock.t - (at + 10) > 120
    assert max(clock.sleeps) <= 5


def test_a_driver_still_running_gives_up_at_120_s_nothing_deleted():
    item = rolled_back_item(OPENED + 200)
    clock = Clock(OPENED + 210)
    driver = {"rolloutId": RID, "pid": 7, "startedAt": iso(OPENED + 5)}
    code, mem, message = run(clock, lambda: item, driver)
    assert code == 5 and mem.deleted == [] and "traffic driver" in message
    assert 120 <= clock.t - (OPENED + 210) <= 125


def test_finished_at_deletes_at_once():
    item = rolled_back_item(OPENED + 200)
    clock = Clock(OPENED + 260)
    driver = {"rolloutId": RID, "pid": 7, "startedAt": iso(OPENED + 5), "finishedAt": iso(OPENED + 250)}
    code, mem, _ = run(clock, lambda: item, driver)
    assert code == 0 and mem.deleted == ["m-1", "m-2"] and clock.sleeps == []


def test_no_driver_state_waits_until_rolled_back_plus_180_s():
    at = OPENED + 200
    item = rolled_back_item(at)
    clock = Clock(at + 5)
    code, mem, _ = run(clock, lambda: item, None)
    assert code == 0 and mem.deleted and clock.t == pytest.approx(at + 180)
    # Another rollout's record is not this rollout's driver state either.
    other = {"rolloutId": "r-ffffffffffffffff", "pid": 9, "startedAt": iso(OPENED), "finishedAt": iso(OPENED + 1)}
    clock = Clock(at + 5)
    code, mem, _ = run(clock, lambda: item, other)
    assert code == 0 and clock.t == pytest.approx(at + 180)


def test_a_ready_time_too_far_ahead_exits_5_at_once():
    item = rolled_back_item(OPENED + 200)
    clock = Clock(OPENED + 210)
    driver = {"rolloutId": RID, "pid": 7, "startedAt": iso(OPENED), "killedAt": iso(OPENED + 400)}   # +370 s ahead
    code, mem, message = run(clock, lambda: item, driver)
    assert code == 5 and mem.deleted == [] and clock.sleeps == [] and "ahead" in message


def test_a_rollout_not_terminal_for_120_s_exits_5():
    item = ddb({**rolled_back_item(OPENED + 200), "state": "OBSERVING"})
    clock = Clock(OPENED + 210)
    code, mem, message = run(clock, lambda: item, None)
    assert code == 5 and mem.deleted == [] and "ROLLED_BACK or PROMOTED" in message


def test_a_malformed_demo_state_exits_5():
    item = rolled_back_item(OPENED + 200)
    for driver in (ValueError("Expecting value: line 1 column 1"), "not an object",
                   {"rolloutId": RID, "startedAt": iso(OPENED), "finishedAt": "2026-10-20T15:00:00"}):
        clock = Clock(OPENED + 210)
        code, mem, message = run(clock, lambda: item, driver)
        assert code == 5 and mem.deleted == [] and "demo-state.json unreadable" in message


def test_a_non_numeric_rolled_back_at_exits_5_with_the_pins_violation():
    item = {**rolled_back_item(OPENED + 200), "rolledBackAt": "yesterday"}
    clock = Clock(OPENED + 210)
    code, mem, message = run(clock, lambda: item, None)
    assert code == 5 and mem.deleted == [] and "not a timestamp" in message


def test_three_read_errors_in_a_row_exit_5():
    clock = Clock(OPENED)

    def broken():
        raise OSError("throttled")
    code, mem, message = run(clock, broken, None)
    assert code == 5 and mem.deleted == [] and "3 control-item read errors" in message


def test_dry_run_never_waits():
    item = rolled_back_item(OPENED + 200)
    clock = Clock(OPENED + 201)
    code, mem, report = run(clock, lambda: item, {"rolloutId": RID, "startedAt": iso(OPENED)}, dry_run=True)
    assert code == 0 and mem.deleted == [] and report.listed == 2 and clock.sleeps == []


def test_purge_touches_only_the_release_namespace(tmp_path):
    memory_id = (tmp_path / "mem").as_uri()
    for release in ("harness-v8", "harness-v7"):
        memory.write_note(memory_id, stage="dev", release=release, text=f"note {release}",
                          metadata={"service": "payments-service", "release": release})
    report = memory.purge(memory_id, stage="dev", release="harness-v8", dry_run=False)
    assert report.deleted == 1
    left = memory.read_notes(memory_id, stage="dev", service="payments-service")
    assert [n["release"] for n in left] == ["harness-v7"]


def test_a_record_the_index_still_lists_after_its_delete_is_not_a_failure():
    """Memory's list index lags deletes: a second purge lists records that are gone, and the delete answers 404."""
    class StaleIndex(FakeMemory):
        def batch_delete_memory_records(self, memoryId, records):
            gone = [r["memoryRecordId"] for r in records if r["memoryRecordId"] == "m-1"]
            self.deleted += [r["memoryRecordId"] for r in records if r["memoryRecordId"] not in gone]
            return {"successfulRecords": [{"memoryRecordId": r} for r in self.deleted],
                    "failedRecords": [{"memoryRecordId": r, "status": "FAILED", "errorCode": 404,
                                       "errorMessage": "Memory record not found"} for r in gone]}
    mem = StaleIndex(["m-1", "m-2"])
    report = memory.purge("mem-1", stage="dev", release="harness-v8", dry_run=False, data_plane=mem)
    assert (report.deleted, report.already_gone, report.failed) == (1, 1, [])


# ---------------------------------------------------------------- reconciliation against S3
NS = "/devops-companion/dev/release/harness-v8/"


class LaggedMemory:
    """Memory whose list index shows a record `lag` seconds after it is written and keeps listing a deleted one
    for `stale` seconds; deleting a record twice answers 404 (both seen live)."""

    def __init__(self, clock, written, lag=60.0, stale=30.0):
        self.clock, self.lag, self.stale = clock, lag, stale
        self.records = dict(written)          # id -> (sessionId, writtenAt)
        self.deleted_at: dict[str, float] = {}

    def list_memory_records(self, **kw):
        assert kw["namespacePath"] == NS
        now = self.clock()
        rows = [{"memoryRecordId": i, "namespaces": [NS], "metadata": {"sessionId": {"stringValue": s}}}
                for i, (s, at) in self.records.items()
                if at + self.lag <= now and (i not in self.deleted_at or now < self.deleted_at[i] + self.stale)]
        return {"memoryRecordSummaries": rows}

    def batch_delete_memory_records(self, memoryId, records):
        ok, failed = [], []
        for r in records:
            i = r["memoryRecordId"]
            if i in self.deleted_at:
                failed.append({"memoryRecordId": i, "status": "FAILED", "errorCode": 404,
                               "errorMessage": "Memory record not found"})
            else:
                self.deleted_at[i] = self.clock()
                ok.append({"memoryRecordId": i})
        return {"successfulRecords": ok, "failedRecords": failed}


class SettleClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def notes_for(sessions):
    return [f"side-effects/dev/harness-v8/{s}/0.json" for s in sessions]


def lagged_purge(written, notes, clock, **kw):
    lines = []
    mem = LaggedMemory(clock, written, **{k: kw.pop(k) for k in ("lag", "stale") if k in kw})
    report = memory.purge("mem-1", stage="dev", release="harness-v8", dry_run=False, data_plane=mem, notes=notes,
                          clock=clock, sleep=clock.sleep, log=lines.append, **kw)
    return report, mem, lines


def test_reconciles_records_the_index_has_not_listed_yet():
    clock = SettleClock()
    written = {f"m-{n}": (f"s-{n}", clock.t - 90 if n < 8 else clock.t - 20) for n in range(10)}   # 2 still lagging
    report, mem, lines = lagged_purge(written, notes_for(f"s-{n}" for n in range(10)), clock)
    assert report.complete and report.deleted == 10 and set(mem.deleted_at) == set(written)
    assert lines[0].startswith("waiting for 2 of 10 notes to reach Memory")
    assert 40 <= report.waited_seconds <= 45                       # the last two list at +40 s


def test_stale_deleted_ids_and_404s_are_not_counted_twice():
    clock = SettleClock()
    written = {"m-1": ("s-1", clock.t - 90), "m-2": ("s-2", clock.t - 90), "m-3": ("s-3", clock.t - 10)}
    report, mem, _ = lagged_purge(written, notes_for(["s-1", "s-2", "s-3"]), clock, stale=300)
    assert report.complete and report.deleted == 3 and report.already_gone == 0 and report.failed == []
    # A re-run while the index still lists all three: each delete answers 404; the earlier purge's record covers them.
    again = memory.purge("mem-1", stage="dev", release="harness-v8", dry_run=False, data_plane=mem,
                         notes=notes_for(["s-1", "s-2", "s-3"]), removed_before={}, clock=clock, sleep=clock.sleep)
    assert again.complete and again.deleted == 0 and again.already_gone == 3 and again.failed == []


def test_reaching_n_at_once_never_waits():
    clock = SettleClock()
    written = {f"m-{n}": (f"s-{n}", clock.t - 120) for n in range(5)}
    report, _, lines = lagged_purge(written, notes_for(f"s-{n}" for n in range(5)), clock)
    assert report.complete and report.deleted == 5 and report.waited_seconds == 0 and lines == []


def test_gives_up_after_120_s_naming_every_note_not_found():
    clock = SettleClock()
    written = {"m-1": ("s-1", clock.t - 90)}                       # s-2's note has no Memory record at all
    notes = notes_for(["s-1", "s-2"])
    report, _, lines = lagged_purge(written, notes, clock)
    assert not report.complete and report.deleted == 1
    assert report.missing == ["side-effects/dev/harness-v8/s-2/0.json"]
    assert report.waited_seconds >= memory.SETTLE_SECONDS and lines[0].startswith("waiting for 1 of 2 notes")


def test_a_rerun_after_a_complete_purge_does_not_wait_for_records_already_gone():
    clock = SettleClock()
    written = {"m-1": ("s-1", clock.t - 90)}
    report, mem, _ = lagged_purge(written, notes_for(["s-1"]), clock, stale=0)
    record = memory.purge_record("dev", "harness-v8", report, datetime.now(timezone.utc))
    again = memory.purge("mem-1", stage="dev", release="harness-v8", dry_run=False, data_plane=mem,
                         notes=notes_for(["s-1"]), removed_before=record["removed"], clock=clock, sleep=clock.sleep)
    assert again.complete and again.waited_seconds == 0 and record["removed"] == {"m-1": "s-1"}


def test_dry_run_lists_once_and_reports_n_from_s3():
    clock = SettleClock()
    written = {"m-1": ("s-1", clock.t - 90), "m-2": ("s-2", clock.t - 5)}
    mem = LaggedMemory(clock, written)
    report = memory.purge("mem-1", stage="dev", release="harness-v8", dry_run=True, data_plane=mem,
                          notes=notes_for(["s-1", "s-2"]), clock=clock, sleep=clock.sleep)
    assert (report.expected, report.listed, report.deleted, mem.deleted_at) == (2, 1, 0, {})


def test_note_session_reads_the_side_effect_key():
    assert memory.note_session("side-effects/prod/harness-v8/m11-prod-candidate-v8-abc/0.json") == \
        "m11-prod-candidate-v8-abc"
    with pytest.raises(ValueError):
        memory.note_session("evaluations/prod/x.json")
