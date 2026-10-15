# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`live_check --demo` / `--armed` items as pure functions over fake reads: items 6 and 7
in both forms, the armed record written only when both pass, item 14's approve->open thresholds (R-b), captures,
quota isolation over the prep chain's locks, and the stage locks reader (demo/locks.py)."""
from __future__ import annotations

import json
import os

from module11.demo import live_check as LC
from module11.demo import locks as L
from module11.demo import state as S
from test_module11_demo_support import EID, RID, T0, TAG, Clock, FakeControl, ddb


def rows(*, approval="InProgress", deploy=None, bake=None, tag=TAG):
    out = [{"stageName": "Seal", "actionName": "Seal", "status": "Succeeded", "startTime": "1",
            "output": {"outputVariables": {"RELEASE_TAG": tag}}},
           {"stageName": "Approval", "actionName": "ReleaseApproval", "status": approval, "startTime": "2"}]
    if deploy:
        out.append({"stageName": "Production", "actionName": "Deploy", "status": deploy, "startTime": "3"})
    if bake:
        out.append({"stageName": "Production", "actionName": "Bake", "status": bake, "startTime": "4"})
    return out


EXECS = [{"pipelineExecutionId": EID, "status": "InProgress"}, {"pipelineExecutionId": "x", "status": "Succeeded"}]


def test_item6_demo_form():
    assert LC.item6({"state": "IDLE"}, schedule="DISABLED", latched=False, outcomes=0, armed=False).ok
    assert not LC.item6({"state": "IDLE"}, schedule="ENABLED", latched=False, outcomes=0, armed=False).ok
    assert not LC.item6({"state": "IDLE"}, schedule="DISABLED", latched=True, outcomes=0, armed=False).ok
    assert not LC.item6({"state": "WARMING"}, schedule="DISABLED", latched=False, outcomes=0, armed=False).ok


def test_item6_armed_form():
    warming = {"state": "WARMING", "rolloutId": RID, "stalled": ["no_data"]}
    good = dict(schedule="ENABLED", latched=False, outcomes=0, armed=True,
                candidate={"status": "READY", "liveVersion": 8}, candidate_version=8)
    result = LC.item6(warming, **good)
    assert result.ok and "stalled is expected" in result.detail
    assert not LC.item6(warming, **{**good, "outcomes": 3}).ok
    assert not LC.item6(warming, **{**good, "schedule": "DISABLED"}).ok
    assert not LC.item6(warming, **{**good, "candidate": {"status": "UPDATING", "liveVersion": 7}}).ok
    assert not LC.item6({"state": "OBSERVING"}, **good).ok


def test_item7_demo_form_requires_the_newest_execution_waiting_with_nothing_behind():
    result, facts = LC.item7(EXECS, rows(), staged_tag=TAG, armed=False)
    assert result.ok and facts == {"pipelineExecutionId": EID, "tag": TAG}
    behind = [EXECS[0], {"pipelineExecutionId": "y", "status": "InProgress"}]
    assert not LC.item7(behind, rows(), staged_tag=TAG, armed=False)[0].ok
    assert not LC.item7(EXECS, rows(), staged_tag=TAG, armed=False, freeze="planned")[0].ok
    assert not LC.item7(EXECS, rows(approval="Succeeded"), staged_tag=TAG, armed=False)[0].ok


def test_item7_armed_form():
    ok, _ = LC.item7(EXECS, rows(approval="Succeeded", deploy="Succeeded", bake="InProgress"), staged_tag=None,
                     armed=True, promote_status="success")
    assert ok.ok
    assert not LC.item7(EXECS, rows(approval="Succeeded", deploy="Succeeded", bake="InProgress"), staged_tag=None,
                        armed=True, promote_status="failed")[0].ok
    assert not LC.item7(EXECS, rows(approval="Succeeded", deploy="InProgress"), staged_tag=None, armed=True,
                        promote_status="success")[0].ok


def test_armed_record_written_only_when_items_6_and_7_pass(tmp_path):
    path = tmp_path / "demo-state.json"
    control = ddb(FakeControl(Clock(), "WARMING").item)        # openedAt as DynamoDB returns it (Decimal epoch)
    facts = {"pipelineExecutionId": EID, "tag": TAG}
    six = LC.CheckResult("6 prod rollout armed", True, "")
    seven = LC.CheckResult("7 release at Production", True, "")
    assert not LC.record_armed([six, LC.CheckResult("7 release at Production", False, "")], facts, control, path)
    assert S.read(path) == {}
    assert LC.record_armed([six, seven], facts, control, path)
    armed = S.read(path)["armed"]
    assert armed["rolloutId"] == RID and armed["pipelineExecutionId"] == EID and armed["tag"] == TAG
    assert armed["openedAt"] == "2026-10-20T14:30:00+00:00"          # T0 - 1800, converted by pins.ts
    assert T0  # the support clock's origin


def test_item14_approve_to_open_thresholds():
    base = dict(detection_s=100, effect_ok=True)
    assert LC.timings_item(approve_to_open_s=7 * 60, **base).ok
    warn = LC.timings_item(approve_to_open_s=8 * 60 + 30, **base)
    assert warn.ok and warn.warning
    assert not LC.timings_item(approve_to_open_s=9 * 60 + 1, **base).ok
    assert not LC.timings_item(approve_to_open_s=300, detection_s=300, effect_ok=True).ok
    assert not LC.timings_item(approve_to_open_s=300, detection_s=100, effect_ok=False).ok
    assert "informational" in LC.timings_item(approve_to_open_s=300, mr_pipeline_s=900, **base).detail


def test_captures_item(tmp_path):
    for b in LC.CAPTURE_BEATS[:-1]:
        (tmp_path / f"{b}.json").write_text("{}")
    result = LC.captures_item(tmp_path)
    assert not result.ok and "b5-canary" in result.detail
    (tmp_path / "b5-canary.json").write_text("{}")
    assert LC.captures_item(tmp_path).ok


def test_locks_reader_and_quota_isolation(tmp_path):
    assert L.read("dev", tmp_path) is None and L.held(tmp_path) == []
    (tmp_path / "dev.lock").write_text(json.dumps({"step": "model-compare", "pid": os.getpid(),
                                                   "startedAt": "2026-10-10T10:00:00+00:00"}))
    (tmp_path / "staging.lock").write_text(json.dumps({"step": "abtest", "pid": 999999, "startedAt": "x"}))
    dev = L.read("dev", tmp_path, alive=lambda pid: pid == os.getpid())
    assert dev.alive and dev.message().startswith("dev is held by model-compare")
    staging = L.read("staging", tmp_path, alive=lambda pid: False)
    assert staging.stale and "stale staging.lock from abtest" in staging.message()
    assert (tmp_path / "staging.lock").exists()                     # never removed
    held = L.held(tmp_path, alive=lambda pid: False)
    assert [lock.name for lock in held] == ["dev", "staging"]
    assert not LC.quota_item(held, []).ok and LC.quota_item([], []).ok
    (tmp_path / "dev.lock").write_text("not json")
    assert L.read("dev", tmp_path).error


def test_quota_isolation_sees_running_gates_calibrations_and_the_chain():
    ps = "\n".join([
        " 101 /bin/bash module11/.local/prep-chain.sh",
        " 102 caffeinate -dimsu module11/.local/prep-chain.sh",
        " 103 /usr/bin/python -m module11 evalset rebaseline --from 2 --to 3",
        " 104 /usr/bin/python -m module11 --root /x traffic --stage prod --sessions 180",
        " 105 /usr/bin/python -m module11 status --all",
        " 106 /usr/bin/python -m module11.demo.live_check --demo"])
    jobs = LC.running_jobs(ps)
    assert jobs == ["prep-chain (pid 101)", "module11 evalset rebaseline (pid 103)", "module11 traffic --stage (pid 104)"]
    assert not LC.quota_item([], jobs).ok and LC.running_jobs("") == []


def test_item8_governance_reads_main_protection_push_rules_and_web_signing():
    main = {"push_access_levels": [{"access_level": 0, "access_level_description": "No one"}],
            "code_owner_approval_required": True, "allow_force_push": False}
    ok, detail = LC.governance_ok(main, {"reject_unsigned_commits": True}, {"web_based_commit_signing_enabled": True})
    assert ok and "No one" in detail
    default = {"push_access_levels": [{"access_level": 40, "access_level_description": "Maintainers"}],
               "code_owner_approval_required": False, "allow_force_push": False}
    ok, detail = LC.governance_ok(default, {"reject_unsigned_commits": None}, {})
    assert not ok and "no direct push" in detail and "reject unsigned" in detail and "web-based signing" in detail
    assert LC.governance_ok(None, {}, {})[0] is False


def test_dev_endpoints_accept_the_armed_candidate_only_for_the_recorded_rollout(monkeypatch):
    """After `demo arm --stage dev` the dev candidate runs ahead of the deployed manifest on purpose (Act 6)."""
    from module11.aws import control_table
    row = {"endpoints": {"candidate": {"status": "READY", "liveVersion": "27", "deployed": 24},
                         "stable": {"status": "READY", "liveVersion": "24", "deployed": 24}}}

    class Session:
        def client(self, name):
            return object()
    monkeypatch.setattr(S, "read", lambda *a, **k: {"devArmed": {"rolloutId": "r-1"}})
    monkeypatch.setattr(control_table, "get", lambda *a, **k: {"rolloutId": "r-1", "state": "WARMING"})
    assert LC._dev_armed_ok(Session(), row)
    monkeypatch.setattr(control_table, "get", lambda *a, **k: {"rolloutId": "r-2", "state": "WARMING"})
    assert not LC._dev_armed_ok(Session(), row)                                   # another rollout
    monkeypatch.setattr(control_table, "get", lambda *a, **k: {"rolloutId": "r-1", "state": "ROLLED_BACK"})
    assert not LC._dev_armed_ok(Session(), row)                                   # no longer open
    monkeypatch.setattr(control_table, "get", lambda *a, **k: {"rolloutId": "r-1", "state": "WARMING"})
    row["endpoints"]["stable"]["liveVersion"] = "25"
    assert not LC._dev_armed_ok(Session(), row)                                   # stable drifted too


class _NoAws:
    def client(self, name):
        return object()


def _prod_row(state: str, rollout_id: str = "r-1") -> dict:
    ahead = state != "READY"
    row = {"stage": "prod", "state": state, "manifestVersion": "mv", "release": "harness-v27",
           "endpoints": {"candidate": {"status": "READY", "liveVersion": "14" if ahead else "13", "deployed": 13,
                                       "ok": not ahead},
                         "stable": {"status": "READY", "liveVersion": "13", "deployed": 13, "ok": True}}}
    if state == "CANARY":
        row["endpoints"]["candidate"]["canary"] = True
        row["rollout"] = {"rolloutId": rollout_id, "state": "WARMING", "canaryPct": 50}
    return row


def test_item4_prod_accepts_the_armed_canary_only_for_the_recorded_rollout(monkeypatch):
    """While the payments release is armed (B5), `status` reports prod CANARY; item 4 passes only when
    demo-state.armed names that rollout."""
    from module11 import cli_live
    monkeypatch.setattr(S, "read", lambda *a, **k: {"armed": {"rolloutId": "r-1"}})
    for row, ok in ((_prod_row("CANARY"), True), (_prod_row("CANARY", "r-2"), False),
                    (_prod_row("NOT READY"), False), (_prod_row("READY"), True)):
        monkeypatch.setattr(cli_live, "stage_status", lambda ctx, stage, row=row: row)
        [result] = LC.check_endpoints(_NoAws(), ["prod"])
        assert result.name == "4 prod endpoints" and result.ok is ok, (row["state"], result.detail)
        assert ("armed payments release, canary 50%" in result.detail) == (ok and row["state"] == "CANARY")
    monkeypatch.setattr(S, "read", lambda *a, **k: {})
    monkeypatch.setattr(cli_live, "stage_status", lambda ctx, stage: _prod_row("CANARY"))
    assert LC.check_endpoints(_NoAws(), ["prod"])[0].ok is False                  # nothing armed


def test_item4_prod_end_to_end_with_the_real_status_rule(monkeypatch):
    """stage_status's CANARY rule (stubbed AWS reads) feeding item 4 for armed prod."""
    from module11 import cli_ops
    from module11.aws import endpoints, params
    current = {"manifestVersion": "mv", "deployment": {"prod": {"runtimeId": "rt", "release": "harness-v27",
                                                                "endpoints": {"candidate": 13, "stable": 13}}}}
    live = {"candidate": {"status": "READY", "liveVersion": "14", "targetVersion": "14"},
            "stable": {"status": "READY", "liveVersion": "13"}}
    monkeypatch.setattr(params, "stage_get", lambda ssm, stage, name: current)
    monkeypatch.setattr(endpoints, "get", lambda ctl, rid, name: live[name])
    monkeypatch.setattr(cli_ops, "_control", lambda ctx, stage: {"state": "WARMING", "rolloutId": "r-1",
                                                                 "canaryPct": 50, "candidateVersion": 14,
                                                                 "stableVersion": 13, "release": "harness-v28"})
    monkeypatch.setattr(cli_ops, "_arm_counts", lambda ctx, stage, rid: {})
    monkeypatch.setattr(S, "read", lambda *a, **k: {"armed": {"rolloutId": "r-1"}})
    [result] = LC.check_endpoints(_NoAws(), ["prod"])
    assert result.ok and "candidate READY live v14 (deployed v13)" in result.detail
    live["candidate"]["liveVersion"] = "15"
    live["candidate"]["targetVersion"] = None
    assert LC.check_endpoints(_NoAws(), ["prod"])[0].ok is False
