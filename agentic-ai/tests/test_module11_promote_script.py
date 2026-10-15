# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""module11/gitlab/promote.sh against a fake `aws` CLI: it releases only its own
execution's approvals, exits at once when Staging failed, releases both approvals for a `required` release,
and stops once the Production deploy succeeded (it never follows Bake)."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "module11/gitlab/promote.sh"
SHA = "b" * 40
OWN, OTHER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"

FAKE_AWS = r'''#!PYTHON
import json, os, sys
import jmespath
state_path = os.environ["FAKE_AWS_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
cmd = args[1] if len(args) > 1 else ""
state.setdefault("calls", []).append(cmd)
if cmd == "list-pipeline-executions":
    doc = {"pipelineExecutionSummaries": state["executions"]}
elif cmd == "list-action-executions":
    exe = opt("--filter").split("=", 1)[1]
    doc = {"actionExecutionDetails": state["actions"].get(exe, [])}
elif cmd == "get-pipeline-state":
    doc = state["pipelineState"]
elif cmd == "put-approval-result":
    state.setdefault("approvals", []).append({"stage": opt("--stage-name"), "action": opt("--action-name"),
                                              "token": opt("--token"), "result": opt("--result")})
    # Approving moves the execution on: mark the approval stage done and, after the last one, the deploy.
    for s in state["pipelineState"]["stageStates"]:
        if s["stageName"] == opt("--stage-name"):
            for a in s["actionStates"]:
                a["latestExecution"]["token"] = None
    if state.get("deployAfter") and len(state["approvals"]) >= state["deployAfter"]:
        state["actions"][state["own"]].append({"stageName": "Production", "actionName": "Deploy", "status": "Succeeded"})
    doc = {"approvedAt": "2026-10-07T00:00:00Z"}
else:
    print("unexpected aws call: " + " ".join(args), file=sys.stderr); sys.exit(9)
json.dump(state, open(state_path, "w"))
query = opt("--query")
out = jmespath.search(query, doc) if query else doc
if opt("--output") == "text":
    print("None" if out is None else (out if not isinstance(out, (dict, list)) else json.dumps(out)))
else:
    print(json.dumps(out))
'''


def _stage(name, action, owner, token):
    return {"stageName": name, "latestExecution": {"pipelineExecutionId": owner, "status": "InProgress"},
            "actionStates": [{"actionName": action, "latestExecution": {"status": "InProgress", "token": token}}]}


def make_state(*, approval_owner=OWN, staging_failed=False, required=False, deploy_after=1):
    stages = [_stage("Approval", "ReleaseApproval", approval_owner, f"tok-{approval_owner[:4]}")]
    if required:
        stages.append(_stage("ExtendedApproval", "ExtendedReleaseApproval", OWN, "tok-ext"))
    actions = {OWN: [{"stageName": "Staging", "actionName": "Gate", "status": "Failed" if staging_failed else "Succeeded"}],
               OTHER: []}
    return {"executions": [{"pipelineExecutionId": OTHER, "sourceRevisions": [{"revisionId": "c" * 40}]},
                           {"pipelineExecutionId": OWN, "sourceRevisions": [{"revisionId": SHA}]}],
            "actions": actions, "pipelineState": {"stageStates": stages}, "own": OWN,
            "deployAfter": deploy_after}


def run(tmp_path, state, level="default", sha=SHA, wait="3"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "aws"
    fake.write_text(FAKE_AWS.replace("#!PYTHON", f"#!{sys.executable}"), encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AWS_STATE": str(state_file), "CI_COMMIT_SHA": sha,
           "CI_COMMIT_TAG": "harness/v3", "CI_JOB_URL": "https://gitlab.example/job/1", "CI_JOB_ID": "1",
           "AWS_TOKEN": "id-token", "M11_PROMOTER_ROLE_ARN": "arn:aws:iam::111122223333:role/m11-gitlab-promoter",
           "M11_WAIT_EXECUTION_S": wait, "M11_WAIT_DEPLOY_S": wait, "GITLAB_USER_LOGIN": "presenter"}
    script = tmp_path / "promote.sh"
    text = SCRIPT.read_text(encoding="utf-8").replace("POLL_S=10", "POLL_S=1")
    script.write_text(text, encoding="utf-8")
    out = subprocess.run(["sh", str(script), level], env=env, capture_output=True, text=True, timeout=60)
    return out, json.loads(state_file.read_text())


pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")


def test_releases_its_own_approval_and_stops_after_the_production_deploy(tmp_path):
    out, state = run(tmp_path, make_state())
    assert out.returncode == 0, out.stderr
    assert [(a["stage"], a["action"], a["token"]) for a in state["approvals"]] == [("Approval", "ReleaseApproval", "tok-1111")]
    assert "approved in gitlab by presenter" in state["approvals"][0]["result"].lower()
    assert "Production deploy succeeded" in out.stdout


def test_never_takes_another_executions_token(tmp_path):
    out, state = run(tmp_path, make_state(approval_owner=OTHER))
    assert out.returncode == 1 and "Timed out" in out.stderr
    assert "approvals" not in state


def test_exits_at_once_when_staging_failed(tmp_path):
    out, state = run(tmp_path, make_state(staging_failed=True))
    assert out.returncode == 1 and "Staging failed" in out.stderr and "approvals" not in state


def test_required_release_releases_both_approvals(tmp_path):
    out, state = run(tmp_path, make_state(required=True, deploy_after=2), level="required")
    assert out.returncode == 0, out.stderr
    assert [a["stage"] for a in state["approvals"]] == ["Approval", "ExtendedApproval"]


def test_production_deploy_failure_fails_the_job(tmp_path):
    state = make_state(deploy_after=99)
    state["actions"][OWN].append({"stageName": "Production", "actionName": "Deploy", "status": "Failed"})
    out, _ = run(tmp_path, state)
    assert out.returncode == 1 and "Production deploy Failed" in out.stderr


@pytest.mark.parametrize("sha,level", [("xyz", "default"), (SHA.upper(), "default"), (SHA, "everything")])
def test_bad_inputs_are_refused_before_any_call(tmp_path, sha, level):
    out, state = run(tmp_path, make_state(), level=level, sha=sha)
    assert out.returncode == 2 and "calls" not in state
