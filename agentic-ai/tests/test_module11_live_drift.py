# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""test_live_drift, opt-in with M11_LIVE_TESTS=1, on dev (M11_LIVE_STAGE):

1. The deployed checker `m11-<stage>-drift-check` reports a clean stage.
2. An unreviewed hotfix as the admin profile (`UpdateAgentRuntimeEndpoint` of `candidate` to an older version) is
   reported, first while UPDATING if the check lands inside that window, then as `live <older> != declared`.
3. `module11 drift revert` waits out UPDATING, retargets as the stage deployer and re-checks clean.
4. A hand-edited role policy is reported as `iam` drift and `release deploy --reapply` (refreshing) reverts it.
Needs no rollout in progress on the stage. Run from agentic-ai/ after `source module11/.local/env.sh`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("M11_LIVE_TESTS") != "1", reason="live tests need M11_LIVE_TESTS=1")

ROOT = Path(__file__).resolve().parents[1]
STAGE = os.environ.get("M11_LIVE_STAGE", "dev")
EVIDENCE = ROOT / "module11/.local/live-drift.json"


@pytest.fixture(scope="module")
def ctx():
    from module11.cli_live import _context
    return _context(ROOT)


def _check(ctx):
    from module11.aws import drift_reads
    from module11.cli_ops import _invoke_drift
    return drift_reads.report_from(_invoke_drift(ctx, STAGE))


def _cli(*args, timeout=1800):
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; live test: this repo's module11 CLI with the test's arguments
    return subprocess.run([sys.executable, "-m", "module11", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=timeout)


def test_live_drift(ctx):
    from module11.aws import control_table, endpoints as EP, params
    from module11.release import naming
    evidence: dict = {"stage": STAGE, "startedAt": time.time()}
    control = control_table.get(ctx.admin.client("dynamodb"), naming.table(STAGE, "control")) or {}
    from module11.release.drift import ACTIVE
    assert control.get("state", "IDLE") not in ACTIVE, f"{STAGE} has a rollout in progress ({control.get('state')})"
    clean = _check(ctx)
    assert clean.drift_count == 0, [f.text() for f in clean.findings]
    current = params.stage_get(ctx.admin.client("ssm"), STAGE, "current")
    dep = current["deployment"][STAGE]
    runtime_id, endpoint = dep["runtimeId"], "candidate" if "candidate" in dep["endpoints"] else STAGE
    declared = int(dep["endpoints"][endpoint])
    older = next(int(v) for v in EP.versions(ctx.admin.client("bedrock-agentcore-control"), runtime_id)
                 if int(v) < declared)
    # 2. The unreviewed hotfix, as the admin profile (not a deployer, not the release tool).
    ctl = ctx.admin.client("bedrock-agentcore-control")
    ctl.update_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint, agentRuntimeVersion=str(older))
    first = _check(ctx)
    hit = [f for f in first.findings if f.component == "endpoint" and f.field == endpoint]
    assert hit, [f.text() for f in first.findings]
    evidence["first"] = [f.text() for f in hit]
    deadline = time.time() + 300
    while ctl.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=endpoint).get("status") == "UPDATING":
        assert time.time() < deadline, "the hotfix never left UPDATING"
        time.sleep(5)
    settled = _check(ctx)
    live = [f for f in settled.findings if f.component == "endpoint" and f.field == endpoint]
    assert live and live[0].observed == f"live {older}" and int(live[0].declared) == declared, \
        [f.text() for f in settled.findings]
    evidence["settled"] = [f.text() for f in live]
    # 3. drift revert as the stage deployer.
    t0 = time.time()
    revert = _cli("drift", "revert", "--stage", STAGE)
    evidence["revert"] = {"exit": revert.returncode, "seconds": round(time.time() - t0, 1),
                          "tail": revert.stdout.strip().splitlines()[-3:]}
    assert revert.returncode == 0, revert.stdout + revert.stderr
    assert _check(ctx).drift_count == 0
    # 4. A hand-edited role policy (an extra harmless statement), then --reapply.
    iam = ctx.admin.client("iam")
    role, name = f"m11-{STAGE}-drift-check", "drift-check"
    doc = iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
    doc = json.loads(doc) if isinstance(doc, str) else doc
    edited = {**doc, "Statement": [*doc["Statement"], {"Sid": "HandEdit", "Effect": "Allow",
                                                       "Action": "logs:DescribeLogGroups", "Resource": "*"}]}
    iam.put_role_policy(RoleName=role, PolicyName=name, PolicyDocument=json.dumps(edited))
    time.sleep(5)
    report = _check(ctx)
    iam_hits = [f for f in report.findings if f.component == "iam" and f.field == f"{role}/{name}"]
    assert iam_hits, [f.text() for f in report.findings]
    evidence["iam"] = [f.text() for f in iam_hits]
    t0 = time.time()
    reapply = _cli("release", "deploy", "--stage", STAGE, "--reapply")
    evidence["reapply"] = {"exit": reapply.returncode, "seconds": round(time.time() - t0, 1)}
    assert reapply.returncode == 0, reapply.stdout[-2000:] + reapply.stderr[-2000:]
    after = _check(ctx)
    assert after.drift_count == 0, [f.text() for f in after.findings]
    evidence.update(finishedAt=time.time(), passed=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
