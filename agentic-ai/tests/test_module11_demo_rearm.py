# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 demo rearm` (module11/demo/rearm.py) against fakes: a control table holding `controller.decide`
items with Decimal numbers, a fake CodePipeline, a fake STS, a fake clock and a fake process table."""
from __future__ import annotations

import signal

import pytest

from module11.demo import rearm as R
from module11.demo import state as S
from test_module11_demo_support import (EID, OPERATOR, PINS, RID, RID2, T0, TAG, Clock, ClientError, FakeControl,
                                        FakePipeline, FakeProcs, Recorder, iso, traffic_record)


class World:
    def __init__(self, tmp_path, *, state="WARMING", profile="demo", tag=TAG, caller=OPERATOR, term="drain",
                 alive=True, survive_kill=False, traffic="running", traffic_rid=RID, production="Failed",
                 rearm_record=False, armed=True, rolled_back_ago=290, open_delay=300, preflight=True):
        self.clock = Clock()
        self.path = tmp_path / "demo-state.json"
        if armed:
            S.update(lambda d: {**d, "armed": {"rolloutId": RID, "pipelineExecutionId": EID, "tag": TAG,
                                               "openedAt": iso(T0 - 1800)}}, self.path)
        if rearm_record:
            S.update(lambda d: {**d, "rearm": {"armedRolloutId": RID, "cleanedAt": iso(T0 - 60)}}, self.path)
        self.control = FakeControl(self.clock, state, rolled_back_ago=rolled_back_ago)
        self.pipe = FakePipeline(self.clock, tag=tag, production=production)
        self.procs = FakeProcs(self.clock, self.path, alive=alive, term=term, survive_kill=survive_kill)
        if traffic == "running":
            traffic_record(self.path, rollout_id=traffic_rid)
        elif traffic == "finished":
            traffic_record(self.path, rollout_id=traffic_rid, started=T0 - 400, finishedAt=iso(T0 - 200),
                           stopReason="complete")
        self.rec = Recorder()
        self.latched = True
        self.caller = caller
        self.caller_calls = 0
        self.caller_fails_on = None
        self.preflight = preflight
        self.pipe.on_retry = lambda: self.control.open_later(open_delay) if open_delay is not None else None
        self.ports = R.Ports(
            stage="prod", state_path=self.path, rollout_profile=lambda: profile, caller_arn=self._caller,
            operator_principals=[OPERATOR], read_control=self.control.read, controller=self._controller,
            latch_clear=self._latch_clear, flags_restore=lambda: self.rec.call("flags restore"),
            candidate_pins=lambda item: PINS, delete_records=self._delete, audit=self._audit,
            alive=self.procs.alive, killpg=self.procs.killpg, clock=self.clock, sleep=self.clock.sleep,
            log=self.rec.log, pipeline=self.pipe, live_check_armed=self._preflight)

    def _caller(self):
        self.caller_calls += 1
        if self.caller_fails_on == self.caller_calls:
            raise ClientError("ExpiredToken")
        return self.caller

    def _controller(self, command, args):
        self.rec.call(command)
        return self.control.invoke(command, args)

    def _latch_clear(self, digest, reason):
        self.rec.call("latch clear", self.latched)
        if self.latched:
            self.rec.call("latch audit")
            self.latched = False

    def _delete(self, release):
        self.rec.call("purge", release, self.clock.t)
        return True

    def _audit(self, kind, fields):
        self.rec.call("audit", kind, fields)
        return f"ledger/prod/{len(self.audits())}.json"

    def _preflight(self):
        self.rec.call("live_check")
        return self.preflight

    def audits(self):
        return [e for e in self.rec.events if e[0] == "audit"]

    def run(self, reason="demo retake"):
        return R.rearm(self.ports, reason)

    def doc(self):
        return S.read(self.path)


def test_warming_start_runs_every_step_in_order_and_re_arms(tmp_path):
    w = World(tmp_path, state="WARMING")
    assert w.run() == 0
    assert w.rec.names() == ["abort", "latch clear", "latch audit", "flags restore", "purge", "reset", "audit",
                             "live_check"]
    assert [s for s, _ in w.procs.signals] == [signal.SIGTERM]
    doc = w.doc()
    assert doc["traffic"]["finishedAt"] and "killedAt" not in doc["traffic"]
    assert len(w.pipe.retries) == 1 and w.pipe.retries[0]["retryMode"] == "ALL_ACTIONS"
    assert w.pipe.retries[0]["stageName"] == "Production" and w.pipe.retries[0]["pipelineExecutionId"] == EID
    assert doc["armed"]["rolloutId"] == RID2 and doc["armed"]["tag"] == TAG and "rearm" not in doc
    kind, fields = w.audits()[0][1:]
    assert kind == "demo_rearm" and fields["priorRolloutId"] == RID and fields["priorState"] == "WARMING"
    assert fields["tag"] == TAG and fields["caller"] == OPERATOR and fields["reason"] == "demo retake"


def test_promoted_start_exits_2_without_cleanup_or_retry(tmp_path):
    w = World(tmp_path, state="PROMOTED", traffic="finished")
    assert w.run() == 2
    assert "latch clear" not in w.rec.names() and not w.pipe.retries and not w.audits()
    assert "full reset cycle" in w.rec.text()


def test_rolled_back_start_sends_no_abort(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    assert w.run() == 0
    assert "abort" not in w.rec.names() and len(w.audits()) == 1 and len(w.pipe.retries) == 1


def test_m1a_driver_ignoring_sigterm_is_killed_and_the_purge_waits_past_120_s(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", term="ignore")
    assert w.run() == 0
    sigs = w.procs.signals
    assert [s for s, _ in sigs] == [signal.SIGTERM, signal.SIGKILL]
    assert sigs[1][1] - sigs[0][1] == pytest.approx(210, abs=0.01)          # stopRequestedAt + deadline + 60
    killed = sigs[1][1]
    assert w.doc()["traffic"]["killedAt"] == iso(killed)
    purge_at = next(e for e in w.rec.events if e[0] == "purge")[2]
    assert purge_at >= killed + 180 and purge_at - killed > 120              # past the 120 s no-time limit
    assert len(w.audits()) == 1 and len(w.pipe.retries) == 1


def test_m1b_driver_surviving_sigkill_gives_up_at_purge(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", term="ignore", survive_kill=True)
    assert w.run() == 4
    assert "killedAt" not in w.doc()["traffic"]
    assert "cleanup incomplete at purge" in w.rec.text()
    assert "purge" not in w.rec.names() and not w.audits() and not w.pipe.retries


def test_driver_already_dead_gets_killed_at_now_and_the_purge_waits_180_s(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", alive=False)
    assert w.run() == 0
    assert w.doc()["traffic"]["killedAt"] == iso(T0)
    purge_at = next(e for e in w.rec.events if e[0] == "purge")[2]
    assert purge_at >= T0 + 180


def test_traffic_for_another_rollout_uses_rolled_back_plus_180_from_the_decimal(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic_rid=RID2, rolled_back_ago=10)
    assert w.run() == 0
    assert not w.procs.signals
    purge_at = next(e for e in w.rec.events if e[0] == "purge")[2]
    assert purge_at == pytest.approx(T0 - 10 + 180, abs=5)
    assert "ROLLED_BACK + deadlineSeconds + 30 s" in w.rec.text()


def test_flags_restore_failure_exits_4_and_a_rerun_resumes_without_a_second_latch_audit(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    w.rec.fail["flags restore"] = RuntimeError("s3 denied")
    assert w.run() == 4
    assert "cleanup incomplete at flags restore" in w.rec.text()
    assert not w.audits() and not w.pipe.retries
    assert w.run() == 0
    assert w.rec.names().count("latch audit") == 1 and len(w.audits()) == 1 and len(w.pipe.retries) == 1


def test_idle_with_a_rearm_record_skips_cleanup_audits_and_retries(tmp_path):
    w = World(tmp_path, state="IDLE", traffic="finished", rearm_record=True)
    assert w.run() == 0
    assert w.rec.names() == ["audit", "live_check"] and len(w.pipe.retries) == 1


@pytest.mark.parametrize("production", ["Failed", "InProgress"])
def test_idle_without_a_rearm_record_exits_2_with_no_writes(tmp_path, production):
    """Includes an armed execution whose m11-deploy failed before open ."""
    w = World(tmp_path, state="IDLE", traffic=None, production=production)
    before = w.path.read_bytes()
    assert w.run() == 2
    assert w.path.read_bytes() == before and not w.rec.events and not w.pipe.retries


@pytest.mark.parametrize("change", [{"profile": "prod"}, {"tag": "harness/v99"},
                                    {"caller": "arn:aws:iam::111122223333:user/someone-else"}])
def test_guards_refuse_with_exit_3_and_no_writes(tmp_path, change):
    w = World(tmp_path, **change)
    before = w.path.read_bytes()
    assert w.run() == 3
    assert w.path.read_bytes() == before and not w.rec.events and not w.procs.signals and not w.pipe.retries


def test_nothing_armed_exits_2(tmp_path):
    w = World(tmp_path, armed=False)
    assert w.run() == 2 and "nothing armed" in w.rec.text()


def test_stage_not_retryable_exits_6_after_exactly_one_audit(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    w.pipe.retry_errors = [ClientError("StageNotRetryableException")]
    assert w.run() == 6
    assert len(w.audits()) == 1 and len(w.pipe.retries) == 1 and "full reset cycle" in w.rec.text()


def test_conflict_on_all_four_calls_exits_4_and_the_rerun_resumes_from_idle(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    w.pipe.retry_errors = [ClientError("ConflictException")] * 4
    assert w.run() == 4
    assert "retry refused for now (ConflictException)" in w.rec.text()
    assert len(w.audits()) == 1 and len(w.pipe.retries) == 4
    assert w.doc()["rearm"]["armedRolloutId"] == RID
    assert w.run() == 0
    assert len(w.audits()) == 2 and len(w.pipe.retries) == 5
    assert w.rec.names().count("reset") == 1                                 # step 4 skipped on the re-run


def test_validation_exception_exits_6_with_its_code_after_one_call(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    w.pipe.retry_errors = [ClientError("ValidationException")]
    assert w.run() == 6
    assert "ValidationException" in w.rec.text() and len(w.pipe.retries) == 1 and len(w.audits()) == 1


def test_credentials_failing_just_before_the_call_exit_4_with_no_call(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    w.caller_fails_on = 3                       # validate, the audit's caller, then the pre-retry check
    assert w.run() == 4
    assert "credentials invalid before the retry" in w.rec.text() and not w.pipe.retries and len(w.audits()) == 1


@pytest.mark.parametrize("opens, code", [(True, 0), (False, 7)])
def test_connection_error_from_the_call_is_not_repeated_and_step_7_decides(tmp_path, opens, code):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")

    def retry(**kw):
        w.pipe.retries.append(kw)
        if opens:
            w.control.open_later(120)
        raise ConnectionError("EndpointConnectionError: could not connect")
    w.pipe.retry_stage_execution = retry
    assert w.run() == code
    assert len(w.pipe.retries) == 1
    assert ("outcome unknown" in w.rec.text())


def test_a_failing_audit_write_exits_5_with_no_retry(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished")
    w.rec.fail["audit"] = RuntimeError("PutObject denied")
    assert w.run() == 5
    assert not w.pipe.retries


def test_no_rollout_within_15_minutes_exits_7_with_armed_unchanged(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished", open_delay=None)
    before = w.doc()["armed"]
    assert w.run() == 7
    assert w.doc()["armed"] == before and "no rollout opened" in w.rec.text()


def test_re_armed_with_a_red_preflight_exits_8_with_armed_updated(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished", preflight=False)
    assert w.run() == 8
    assert w.doc()["armed"]["rolloutId"] == RID2


def test_rollout_that_never_rolls_back_exits_4(tmp_path):
    w = World(tmp_path, state="WARMING")
    w.control.never_rolls_back = True
    assert w.run() == 4
    assert "did not reach ROLLED_BACK" in w.rec.text() and not w.audits()


def test_stage_never_failing_exits_4_after_15_minutes(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="finished", production="InProgress")
    assert w.run() == 4
    assert "not Failed after 15 minutes" in w.rec.text() and "latch clear" not in w.rec.names()


def test_stage_status_reads_the_newest_attempt_per_action():
    rows = [{"stageName": "Production", "actionName": "Bake", "status": "Failed", "startTime": "1"},
            {"stageName": "Production", "actionName": "Bake", "status": "InProgress", "startTime": "2"},
            {"stageName": "Production", "actionName": "Deploy", "status": "Succeeded", "startTime": "2"}]
    assert R.stage_status(rows) == "InProgress"
    assert R.stage_status(rows[:1]) == "Failed"
    assert R.stage_status([]) == "NotStarted"
