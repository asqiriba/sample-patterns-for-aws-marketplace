# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 demo arm --stage dev [--rearm]` (module11/demo/arm.py) against the same fakes as demo rearm:
argparse refusals before any client, exit-3 guards with no writes, --rearm's order and
resume, the SIGKILL/killedAt purge wait, the survive-SIGKILL give-up, and audit before deploy."""
from __future__ import annotations

import json
import os
import signal

import pytest

from module11.demo import arm as A
from module11.demo import state as S
from test_module11_demo_support import (DIGEST, OPERATOR, PINS, RID, RID2, T0, Clock, FakeControl, FakeProcs,
                                        Recorder, iso, traffic_record)

BAD = "sha256:" + "cd" * 32


class World:
    def __init__(self, tmp_path, *, state="IDLE", profile="demo", caller=OPERATOR, term="drain", survive_kill=False,
                 traffic=None, lock=None, latched=False, current=DIGEST):
        self.clock = Clock()
        self.path = tmp_path / "demo-state.json"
        self.locks = tmp_path / "locks"
        self.locks.mkdir()
        if lock:
            (self.locks / "dev.lock").write_text(json.dumps(lock), encoding="utf-8")
        S.update(lambda d: {**d, "note": "x"}, self.path)
        self.control = FakeControl(self.clock, state, open_args={"behaviorDigest": BAD})
        self.procs = FakeProcs(self.clock, self.path, term=term, survive_kill=survive_kill)
        if traffic == "running":
            traffic_record(self.path)
        self.rec = Recorder()
        self.is_latched = latched
        self.ports = A.ArmPorts(
            stage="dev", state_path=self.path, rollout_profile=lambda: profile, caller_arn=lambda: caller,
            operator_principals=[OPERATOR], read_control=self.control.read, controller=self._controller,
            latch_clear=self._latch_clear, flags_restore=lambda: self.rec.call("flags restore"),
            candidate_pins=lambda item: PINS, delete_records=self._delete, audit=self._audit,
            alive=self.procs.alive, killpg=self.procs.killpg, clock=self.clock, sleep=self.clock.sleep,
            log=self.rec.log, lock_dir=self.locks, lock_alive=lambda pid: pid == os.getpid(),
            bad_digest=lambda: BAD, current_digest=lambda: current, latched=lambda d: self.is_latched,
            deploy_bad=self._deploy)

    def _controller(self, command, args):
        self.rec.call(command)
        return self.control.invoke(command, args)

    def _latch_clear(self, digest, reason):
        self.rec.call("latch clear")
        self.is_latched = False

    def _delete(self, release):
        self.rec.call("purge", self.clock.t)
        return True

    def _audit(self, kind, fields):
        self.rec.call("audit", kind, fields)
        return "ledger/dev/1.json"

    def _deploy(self):
        self.rec.call("deploy")
        self.control.item = {**self.control.item, "state": "IDLE"} if self.control.item.get("state") != "IDLE" \
            else self.control.item
        self.control.invoke("open", {"rolloutId": RID2, "profile": "demo", "stableVersion": 7, "candidateVersion": 9,
                                     "behaviorDigest": BAD, "manifestVersion": "mv", "release": "harness-dev"})
        return self.control.read()

    def run(self, **kw):
        return A.arm(self.ports, **kw)


@pytest.mark.parametrize("stage", ["prod", "staging"])
def test_stage_other_than_dev_exits_2_before_any_client(stage, monkeypatch):
    from module11 import cli
    import module11.cli_live as live
    monkeypatch.setattr(live, "_context", lambda root: pytest.fail("a client was created"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["demo", "arm", "--stage", stage])
    assert exc.value.code == 2


@pytest.mark.parametrize("change, message", [
    ({"profile": "prod"}, "outside the demo rollout profile"),
    ({"caller": "arn:aws:iam::111122223333:user/x"}, "not an operator principal"),
    ({"lock": {"step": "model-compare", "pid": os.getpid(), "startedAt": "2026-10-20T14:00:00+00:00"}},
     "dev is held by model-compare"),
    ({"lock": {"step": "model-compare", "pid": 999999, "startedAt": "2026-10-20T14:00:00+00:00"}},
     "stale dev.lock from model-compare"),
    ({"state": "WARMING"}, "use --rearm"),
    ({"latched": True}, "latched on dev; use --rearm"),
    ({"current": BAD}, "dev already serves the bad release"),
])
def test_guards_exit_3_with_no_writes(tmp_path, change, message):
    w = World(tmp_path, **change)
    before = w.path.read_bytes()
    assert w.run() == 3
    assert message in w.rec.text()
    assert w.path.read_bytes() == before and not w.rec.events
    if "lock" in change:
        assert (w.locks / "dev.lock").exists()                         # never removed automatically


def test_happy_path_audits_once_before_one_deploy_then_writes_dev_armed(tmp_path):
    w = World(tmp_path)
    assert w.run() == 0
    assert w.rec.names() == ["audit", "deploy"]
    kind, fields = w.rec.events[0][1:]
    assert kind == "demo_arm" and fields["stage"] == "dev" and fields["behaviorDigest"] == BAD
    armed = S.read(w.path)["devArmed"]
    assert armed["rolloutId"] == RID2 and armed["digest"] == BAD and armed["openedAt"].endswith("+00:00")


def test_rearm_runs_its_six_actions_in_order_and_resumes_after_exit_4(tmp_path):
    w = World(tmp_path, state="WARMING", traffic="running")
    w.rec.fail["flags restore"] = RuntimeError("denied")
    assert w.run(rearm=True) == 4
    assert "cleanup incomplete at flags restore" in w.rec.text() and "nothing deployed" in w.rec.text()
    assert w.rec.names() == ["abort", "latch clear", "flags restore"]
    assert [s for s, _ in w.procs.signals] == [signal.SIGTERM]
    assert w.run(rearm=True) == 0
    assert w.rec.names()[3:] == ["latch clear", "flags restore", "purge", "reset", "audit", "deploy"]


def test_rearm_with_a_driver_ignoring_sigterm_kills_it_and_waits_until_killed_at_plus_180(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="running", term="ignore")
    assert w.run(rearm=True) == 0
    kill_at = next(t for s, t in w.procs.signals if s == signal.SIGKILL)
    assert S.read(w.path)["traffic"]["killedAt"] == iso(kill_at)
    purge_at = next(e for e in w.rec.events if e[0] == "purge")[1]
    assert purge_at >= kill_at + 180
    assert w.rec.names()[-2:] == ["audit", "deploy"] and w.rec.names().count("audit") == 1


def test_rearm_with_a_driver_surviving_sigkill_gives_up_with_no_audit_or_deploy(tmp_path):
    w = World(tmp_path, state="ROLLED_BACK", traffic="running", term="ignore", survive_kill=True)
    assert w.run(rearm=True) == 4
    assert "cleanup incomplete at purge" in w.rec.text()
    assert "audit" not in w.rec.names() and "deploy" not in w.rec.names()


def test_a_failing_audit_write_exits_5_with_no_deploy(tmp_path):
    w = World(tmp_path)
    w.rec.fail["audit"] = RuntimeError("denied")
    assert w.run() == 5
    assert "deploy" not in w.rec.names() and "devArmed" not in S.read(w.path)


def test_a_deploy_error_exits_6(tmp_path):
    w = World(tmp_path)
    w.rec.fail["deploy"] = RuntimeError("pulumi up failed")
    assert w.run() == 6 and "devArmed" not in S.read(w.path)
