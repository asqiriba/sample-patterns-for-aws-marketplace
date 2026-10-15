# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Experiment-path live checks, opt-in with M11_LIVE_TESTS=1, on dev:
- test_live_shadow_experiment: the full shadow-experiment sequence with `--hours 0.25`. A GitLab
  branch with an instructions change is sealed in m11-seal-experiment (no tag or package), deployed as the
  experiment (candidate only, shadowed at 100%), driven by `traffic --rate 20`, given `shadow-evaluate` after 15
  minutes, and finished. Also checks who can start the seal, the operator role's override and trust limits, a bad
  SOURCE_SHA, the state afterwards, and that the bundle version hashes to the sealed component.
- test_live_shadow_batch: the same run's verdict came from the controller's two batch
  evaluations. It records the controller role's log permissions that StartBatchEvaluation used, and
  every shadow-worker session has its invoke_agent span in the candidate endpoint's log group within 6 minutes.
The experiment runs once (module fixture); both tests read its record, which is also written to
module11/.local/experiment-live.json. The branch `m11-experiment/<id>` is created in the harness project and deleted
at the end. Run from agentic-ai/ after `source module11/.local/env.sh`.
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
LOCAL = ROOT / "module11/.local"
CLONE = LOCAL / "harness-clone"
STAGE = "dev"
HOURS = 0.25
PROMPT = "module11/harness/agent/prompts/companion.md"
EDIT = ("Keep the diagnosis brief: at most 60 words.", "Keep the diagnosis brief: at most 55 words.")
SPAN_LIMIT_SECONDS = 360


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(CLONE), *args], check=True, capture_output=True, text=True).stdout.strip()


def gitlab(ctx):
    from module11.aws import gitlab_api as G
    return G.from_secret(ctx.admin.client("secretsmanager"), os.environ.get("M11_GITLAB_TOKEN_SECRET",
                         "devops-companion/partners/gitlab-token"), os.environ["M11_GITLAB_PROJECT_ID"])


def control(ctx) -> dict:
    from module11.aws import control_table
    from module11.release import naming
    return control_table.get(ctx.admin.client("dynamodb"), naming.table(STAGE, "control")) or {}


def experiment_objects(ctx, xid: str) -> list[str]:
    from module11.cli_experiment import PREFIX
    s3 = ctx.admin.client("s3")
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=ctx.release_bucket, Prefix=f"{PREFIX}/"):
        keys += [o["Key"] for o in page.get("Contents") or [] if xid in o["Key"]]
    return keys


def start_seal(session, region: str, **overrides):
    cb = session.client("codebuild", region_name=region)
    return cb.start_build(projectName="m11-seal-experiment", **overrides)


def operator_session(ctx):
    from module11.aws import operator
    return operator.assume(ctx.role_arn("m11-operator-experiment"), sts=ctx.admin.client("sts"),
                           purpose="live experiment checks", stage="shared",
                           record=operator.local_record(LOCAL / "audit"))


# ---------------------------------------------------------------- the run (once)
@pytest.fixture(scope="module")
def ctx():
    from module11.cli_live import _context
    return _context(ROOT)


@pytest.fixture(scope="module")
def run(ctx):
    from module11.cli_experiment import Experiment
    state = control(ctx).get("state", "IDLE")
    assert state in ("IDLE", "PROMOTED", "ROLLED_BACK"), f"dev rollout is {state}"
    branch = f"m11-experiment/live-{int(time.time())}"
    head = git("rev-parse", "origin/main")
    git("checkout", "-q", "-b", branch, "origin/main")
    try:
        path = CLONE / PROMPT
        text = path.read_text(encoding="utf-8")
        assert EDIT[0] in text
        path.write_text(text.replace(EDIT[0], EDIT[1], 1), encoding="utf-8")
        git("add", PROMPT)
        git("commit", "-q", "-m", "Experiment: a 55-word diagnosis limit (shadow only, never merged)")
        git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    finally:
        git("checkout", "-q", "main")
    record = {"branch": branch, "base": head, "startedAt": time.time()}
    exp = Experiment(ROOT)
    traffic = None
    try:
        doc = exp.start(branch, STAGE, HOURS)
        record.update(experiment=doc["id"], rolloutId=doc["rolloutId"], manifestVersion=doc["manifestVersion"],
                      candidateVersion=doc["candidateVersion"], sourceSha=doc["sourceSha"])
        log = (LOCAL / f"traffic-{doc['id']}.log").open("w")
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; live test: this repo's module11 CLI, fixed stage, the rollout ID it opened
        traffic = subprocess.Popen([sys.executable, "-m", "module11", "traffic", "--stage", STAGE, "--until-terminal",
                                    "--rate", "20", "--rollout-id", doc["rolloutId"], "--max-minutes", "90"],
                                   cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        record["shadowingItem"] = control(ctx)
        done = exp.wait(doc["id"])
        record.update(verdict=done["verdict"], batchIds=done.get("batchIds"), scoredPairs=done.get("scoredPairs"))
        item = control(ctx)
        record["verdictItem"] = {k: item.get(k) for k in ("state", "rolloutId", "experiment", "shadow")}
        record["bundle"] = bundle_check(ctx, doc)
        record["outcomes"] = outcome_check(ctx, doc["rolloutId"])
        record["spans"] = span_check(ctx, doc["rolloutId"])
    finally:
        if not record.get("experiment"):
            # A failed start leaves its freeze; finish the newest local record (reapply, unfreeze).
            newest = max((LOCAL / "experiments").glob("x-*.json"), key=lambda p: p.stat().st_mtime, default=None)
            if newest is not None and newest.stat().st_mtime >= record["startedAt"]:
                record["experiment"] = newest.stem
        if record.get("experiment"):
            exp.finish(record["experiment"])
            record["finishedAt"] = time.time()
        if traffic is not None:
            try:
                traffic.wait(timeout=600)
            except subprocess.TimeoutExpired:
                traffic.terminate()
        try:
            gitlab(ctx).delete_branch(branch)
        except Exception as exc:  # noqa: BLE001 - recorded; the branch is harmless if left
            record["branchDeleteError"] = type(exc).__name__
        (LOCAL / "experiment-live.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def bundle_check(ctx, doc: dict) -> dict:
    """The experiment's bundle version, read back from the service, hashes to the sealed component."""
    from module11.aws import bundles, params, releases
    from module11.release import normalize as N
    from module11.release.hashing import sha256_bytes
    ssm = ctx.admin.client("ssm")
    candidate = params.stage_get(ssm, STAGE, "candidate") or {}
    dep = candidate["deployment"][STAGE]
    version = bundles.get_version(ctx.admin.client("bedrock-agentcore-control"), dep["bundle"]["arn"],
                                  dep["bundle"]["versionId"])
    seal = json.loads(releases.get(ctx.admin.client("s3"),
                                   f"s3://{ctx.release_bucket}/releases/experiments/{doc['manifestVersion']}/seal.json"))
    return {"versionId": dep["bundle"]["versionId"], "sealed": seal["bundleComponent"]["sha256"],
            "readBack": sha256_bytes(N.canonical_json(bundles.component_of(version)))}


def outcome_check(ctx, rollout_id: str) -> dict:
    from module11.aws import control_table
    from module11.release import naming
    items = control_table.outcomes(ctx.admin.client("dynamodb"), naming.table(STAGE, "outcomes"), rollout_id)
    exposed = [i for i in items if i.get("arm") == "candidate" and not i.get("shadow")]
    return {"items": len(items), "shadow": sum(1 for i in items if i.get("shadow")), "candidateNotShadow": len(exposed),
            "shadowSessions": sorted({i["runtimeSessionId"] for i in items if i.get("shadow")}),
            "shadowEnded": [int(i["sk"].split("#", 1)[0]) for i in items if i.get("shadow")]}


def span_check(ctx, rollout_id: str) -> dict:
    """Every shadow-worker session has an invoke_agent span in the candidate endpoint's log group (the group the
    controller's batch evaluation reads) within SPAN_LIMIT_SECONDS of its end. The runtime's own spans land there;
    `aws/spans` holds the Lambda and edge spans."""
    from module11.aws import spans as SP
    from module11.release import eval_names
    shadow = outcome_check(ctx, rollout_id)
    if not shadow["shadowSessions"]:
        return {"sessions": 0}
    latest = max(shadow["shadowEnded"]) / 1000.0
    wait = latest + SPAN_LIMIT_SECONDS - time.time()
    if wait > 0:
        time.sleep(wait)
    env = ctx.admin.client("lambda").get_function_configuration(FunctionName=f"m11-{STAGE}-controller")
    group = eval_names.runtime_log_group(env["Environment"]["Variables"]["RUNTIME_ID"], "candidate")
    logs = ctx.admin.client("logs")
    counts = SP.readiness_counts(logs, group, f"m11-{STAGE}-shadow-v", min(shadow["shadowEnded"]) / 1000.0 - 600,
                                 time.time())
    missing = [s for s in shadow["shadowSessions"] if counts.get(s, (0, 0))[1] < 1]
    return {"sessions": len(shadow["shadowSessions"]), "missing": missing}


# ---------------------------------------------------------------- tests
def test_live_shadow_experiment(ctx, run):
    from module11.aws import params
    from module11.cli_experiment import freeze_reason
    from module11.infrastructure.platform import pipeline_roles as PR
    from module11.release import naming
    assert run["verdict"]
    # The seal created no tag: no harness/v* tag points at the experiment commit.
    tags = gitlab(ctx).get(f"/projects/{os.environ['M11_GITLAB_PROJECT_ID']}/repository/tags?search=harness/v")
    assert all((t.get("commit") or {}).get("id") != run["sourceSha"] for t in tags)
    assert experiment_objects(ctx, run["experiment"])            # sealed under releases/experiments/ only
    # Shadow only: no candidate outcome item that a user saw.
    assert run["outcomes"]["candidateNotShadow"] == 0 and run["outcomes"]["shadow"] > 0
    # The bundle version the experiment deployed is the sealed component (deployed from a checkout of main).
    assert run["bundle"]["readBack"] == run["bundle"]["sealed"]
    # Afterwards: dev back on /current, drift clean, freeze none.
    ssm = ctx.admin.client("ssm")
    assert params.stage_get(ssm, STAGE, "applied")["manifestVersion"] == \
        params.stage_get(ssm, STAGE, "current")["manifestVersion"]
    assert params.freeze(ssm).get("state") == "none"
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; live test: this repo's module11 CLI with a fixed stage
    report = subprocess.run([sys.executable, "-m", "module11", "drift", "check", "--stage", STAGE], cwd=ROOT,
                            capture_output=True, text=True)
    assert "0 finding(s)" in report.stdout, report.stdout[-1500:]
    # Who can start the seal: not the pipeline role, the stage deployers or the GitLab CI role.
    iam = ctx.admin.client("iam")
    project = f"arn:aws:codebuild:{ctx.region}:{ctx.account}:project/m11-seal-experiment"
    for role in [PR.PIPELINE_ROLE, "m11-gitlab-ci"] + [naming.deployer_role(s) for s in naming.STAGES]:
        result = iam.simulate_principal_policy(PolicySourceArn=ctx.role_arn(role), ActionNames=["codebuild:StartBuild"],
                                               ResourceArns=[project])["EvaluationResults"][0]
        assert result["EvalDecision"] != "allowed", role
    # As m11-operator-experiment: a third override name and a buildspec override are denied.
    session = operator_session(ctx)
    from botocore.exceptions import ClientError
    good = [{"name": "SOURCE_SHA", "value": "0" * 40, "type": "PLAINTEXT"},
            {"name": "EXPERIMENT_ID", "value": "x-" + "0" * 16, "type": "PLAINTEXT"}]
    for overrides in ({"environmentVariablesOverride": good + [{"name": "SEAL_MODE", "value": "release",
                                                                 "type": "PLAINTEXT"}]},
                      {"environmentVariablesOverride": good, "buildspecOverride": "version: 0.2"}):
        with pytest.raises(ClientError) as denied:
            start_seal(session, ctx.region, **overrides)
        assert denied.value.response["Error"]["Code"] in ("AccessDeniedException", "AccessDenied")
    # A SOURCE_SHA that fails its pattern ends the build with ExperimentRefused and writes nothing.
    xid = "x-" + "f" * 16
    build = start_seal(session, ctx.region, environmentVariablesOverride=[
        {"name": "SOURCE_SHA", "value": "not-a-sha", "type": "PLAINTEXT"},
        {"name": "EXPERIMENT_ID", "value": xid, "type": "PLAINTEXT"}])["build"]
    cb = session.client("codebuild", region_name=ctx.region)
    while True:
        time.sleep(10)
        status = cb.batch_get_builds(ids=[build["id"]])["builds"][0]
        if status["buildStatus"] != "IN_PROGRESS":
            break
    assert status["buildStatus"] == "FAILED"
    group, stream = status["logs"]["groupName"], status["logs"]["streamName"]
    text = " ".join(e["message"] for e in ctx.admin.client("logs").get_log_events(
        logGroupName=group, logStreamName=stream)["events"])
    assert 'ExperimentRefused("invalid input")' in text and "not-a-sha" not in text   # never echoed
    assert experiment_objects(ctx, xid) == []
    # Operator trust: a session name outside m11-operator-*, or no source identity, is denied.
    sts = ctx.admin.client("sts")
    with pytest.raises(ClientError):
        sts.assume_role(RoleArn=ctx.role_arn("m11-operator-experiment"), RoleSessionName="someone-else",
                        SourceIdentity="m11-live-test")
    with pytest.raises(ClientError):
        sts.assume_role(RoleArn=ctx.role_arn("m11-operator-experiment"), RoleSessionName="m11-operator-live-test")
    assert freeze_reason(run["experiment"]) == f"experiment {run['experiment']}"


def test_live_shadow_batch(ctx, run):
    item = run["verdictItem"]
    assert item["experiment"] and item["shadow"]["verdict"]
    ids = item["shadow"].get("batchIds") or run.get("batchIds") or {}
    assert len(ids) == 2, ids                                   # both batch evaluations, from the controller
    assert int(run["scoredPairs"] or 0) > 0
    # The log permissions the controller's role holds, which StartBatchEvaluation ran with.
    iam = ctx.admin.client("iam")
    lam = ctx.admin.client("lambda")
    role_arn = lam.get_function_configuration(FunctionName=f"m11-{STAGE}-controller")["Role"]
    name = role_arn.rsplit("/", 1)[-1]
    statements = []
    for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=name, PolicyName=policy)["PolicyDocument"]
        for s in doc["Statement"] if isinstance(doc["Statement"], list) else [doc["Statement"]]:
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            if any(a.startswith("logs:") for a in actions):
                statements.append({"Sid": s.get("Sid"), "Action": actions, "Resource": s.get("Resource")})
    (LOCAL / "u11b-batch-log-permissions.json").write_text(json.dumps(statements, indent=2), encoding="utf-8")
    assert statements
    # Sampling: every shadow-worker session has its invoke_agent span within 6 minutes.
    assert run["spans"]["sessions"] > 0 and run["spans"]["missing"] == [], run["spans"]
