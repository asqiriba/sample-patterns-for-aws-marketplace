# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Kill switch, evaluated through Module 10's LocalLaunchDarkly."""
import json
from pathlib import Path

import pytest

from module11.release import flags_edit as F

ROOT = Path(__file__).resolve().parents[1]
DOC = json.loads((ROOT / F.FLAGS_PATH).read_text(encoding="utf-8"))
KEY = "module10-recovery-planning"


def test_kill_moves_the_team_to_variation_0_and_bumps_the_version():
    killed, changed = F.kill(DOC, KEY, "payments")
    flag = killed["flags"][KEY]
    assert changed and flag["version"] == DOC["flags"][KEY]["version"] + 1
    on = [t for t in flag["contextTargets"] if t["variation"] == 1][0]
    off = [t for t in flag["contextTargets"] if t["variation"] == 0][0]
    assert on["values"] == ["platform"] and off["values"] == ["payments"]
    assert DOC["flags"][KEY]["contextTargets"][0]["values"] == ["platform", "payments"]   # input untouched
    again, changed = F.kill(killed, KEY, "payments")
    assert not changed and again == killed
    assert F.flags_hash(killed) != F.flags_hash(DOC)


def test_restore_and_etag_retry():
    killed, _ = F.kill(DOC, KEY, "payments")
    store = {"doc": killed, "etag": "e1", "fail": 2, "puts": 0}

    def get():
        return json.loads(json.dumps(store["doc"])), store["etag"]

    def put(body, etag):
        store["puts"] += 1
        if store["fail"]:
            store["fail"] -= 1
            store["etag"] = f"e{store['puts'] + 1}"       # someone else wrote in between
            raise F.PreconditionFailed()
        assert etag == store["etag"]
        store["doc"] = json.loads(body)

    doc, changed, attempts = F.edit_with_retry(get, put, F.restore_to(DOC))
    assert changed and attempts == 3 and store["doc"] == DOC
    assert F.edit_with_retry(get, put, F.restore_to(DOC))[1] is False
    store["fail"] = 10
    with pytest.raises(F.FlagEditConflict):
        F.edit_with_retry(get, put, lambda d: F.kill(d, KEY, "platform"))
    assert store["puts"] == 2 + 1 + 4


def test_killed_document_evaluates_through_local_launchdarkly(tmp_path):
    pytest.importorskip("ldclient")
    from module10.flags import LocalLaunchDarkly
    killed, _ = F.kill(DOC, KEY, "payments")
    path = tmp_path / "flags.json"
    path.write_bytes(F.render(killed))
    ld = LocalLaunchDarkly(path)
    try:
        assert ld.evaluate("payments")["recovery_planning_enabled"] is False
        assert ld.evaluate("platform")["recovery_planning_enabled"] is True
        assert ld.evaluate("payments")["version"] == killed["flags"][KEY]["version"]
    finally:
        ld.close()


def test_audit_record():
    killed, _ = F.kill(DOC, KEY, "payments")
    record = F.audit_record("kill", KEY, "payments", "prod", DOC, killed, "operator", "2026-10-13T19:20:00Z")
    assert record["action"] == "flags_kill" and record["details"]["after"] == F.flags_hash(killed)
