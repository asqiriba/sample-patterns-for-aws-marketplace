# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""test_live_preview_boundary, opt-in with M11_LIVE_TESTS=1.

Needs two open MRs with deployed preview stacks: M11_LIVE_MR_A and M11_LIVE_MR_B (iids). Run from agentic-ai/
after `source module11/.local/env.sh`. How each property is exercised:
- m11-gitlab-ci (GitLab OIDC trust only) and the CodeBuild roles m11-preview / m11-preview-infra cannot be assumed
  from a laptop, so their identity policies are evaluated with iam:SimulatePrincipalPolicy, passing the condition
  keys a StartBuild override or a resource tag would set (an earlier probe made the same calls for real against an
  identical policy).
- m11-preview-deployer (the role every preview deploy runs as) is assumed through the operator trust and makes the
  forbidden calls for real; each must be AccessDenied.
- The end-to-end path is real: an authenticated POST /invoke through the mr-<a> edge with a token from Module 10's
  terminal client, GET /requests/{id}, and one Evaluate as the mr-<a> eval-runner role.
- The cross-MR destroy is the trusted m11-preview build itself, started with the three variables a job passes
  (the starter's identity does not change its checks). Stopping MR b's environment runs its on_stop job
  (preview:destroy), which also records whether GitLab reports `stopping` while that job runs.
  This last case destroys mr-<b>; it runs only with M11_LIVE_DESTROY_B=1.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("M11_LIVE_TESTS") != "1", reason="live tests need M11_LIVE_TESTS=1")
ROOT = Path(__file__).resolve().parents[1]
MR_A = os.environ.get("M11_LIVE_MR_A", "")
MR_B = os.environ.get("M11_LIVE_MR_B", "")

# The 21 StartBuild condition keys m11-gitlab-ci denies, one Deny statement each.
FORBIDDEN = ["codebuild:source", "codebuild:source.buildspec", "codebuild:source.location",
             "codebuild:source.auth.type", "codebuild:source.insecureSsl", "codebuild:secondarySources",
             "codebuild:secondaryArtifacts", "codebuild:artifacts", "codebuild:cache", "codebuild:logsConfig",
             "codebuild:serviceRole", "codebuild:encryptionKey", "codebuild:autoRetryLimit",
             "codebuild:environment.image", "codebuild:environment.type", "codebuild:environment.computeType",
             "codebuild:environment.privilegedMode", "codebuild:environment.certificate",
             "codebuild:environment.registryCredential", "codebuild:environment.imagePullCredentialsType",
             "codebuild:environment.fleet.fleetArn"]
ALLOWED_NAMES = ["PREVIEW_ACTION", "PREVIEW_IID", "PREVIEW_SHA"]
TOKEN_SECRET = "devops-companion/partners/gitlab-token"  # nosec B105 - a Secrets Manager secret name, not a secret value
DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


@pytest.fixture(scope="module")
def ctx():
    from module11.cli_live import _context
    return _context(ROOT)


def _need_mrs(*names):
    missing = [n for n, v in (("M11_LIVE_MR_A", MR_A), ("M11_LIVE_MR_B", MR_B)) if n in names and not v]
    if missing:
        pytest.skip(f"set {', '.join(missing)} to the iids of open MRs with preview stacks")


def _arn(ctx, kind: str, rest: str) -> str:
    return f"arn:aws:{kind}:{ctx.region}:{ctx.account}:{rest}"


def _simulate(ctx, role: str, action: str, resource: str, context: dict | None = None) -> str:
    from module11.demo.live_check import _simulate as sim
    return sim(ctx.admin.client("iam"), f"arn:aws:iam::{ctx.account}:role/{role}", action, resource, context)


def _current(ctx, stage: str) -> dict:
    from module11.aws import params
    current = params.stage_get(ctx.admin.client("ssm"), stage, "current")
    assert current, f"{stage} has no /current"
    return current["deployment"][stage]


def _denied(call) -> str:
    from botocore.exceptions import ClientError
    try:
        call()
    except ClientError as exc:
        return exc.response["Error"]["Code"]
    return "allowed"


# ---------------------------------------------------------------- the CI role's StartBuild boundary
def test_live_preview_boundary_ci_overrides(ctx):
    """21 forbidden overrides are explicit Denies; the three variables (and no override) are allowed; another
    variable name has no Allow; the CI role can start no other project."""
    project = _arn(ctx, "codebuild", "project/m11-preview")
    results = {key: _simulate(ctx, "m11-gitlab-ci", "codebuild:StartBuild", project, {key: "x"}) for key in FORBIDDEN}
    assert all(v == "explicitDeny" for v in results.values()), {k: v for k, v in results.items() if v != "explicitDeny"}
    name_key = "codebuild:environment.environmentVariables.name"
    assert _simulate(ctx, "m11-gitlab-ci", "codebuild:StartBuild", project, {name_key: ALLOWED_NAMES}) == "allowed"
    assert _simulate(ctx, "m11-gitlab-ci", "codebuild:StartBuild", project, {name_key: ALLOWED_NAMES[:1]}) == "allowed"
    assert _simulate(ctx, "m11-gitlab-ci", "codebuild:StartBuild", project,
                     {name_key: ALLOWED_NAMES + ["EVIL"]}) == "implicitDeny"
    other = _arn(ctx, "codebuild", "project/m11-deploy")
    assert _simulate(ctx, "m11-gitlab-ci", "codebuild:StartBuild", other, {name_key: ALLOWED_NAMES}) != "allowed"
    print("ci overrides:", json.dumps({"denied": len(results), "allowedNames": ALLOWED_NAMES}))


# ---------------------------------------------------------------- preview roles
def test_live_preview_boundary_preview_roles(ctx):
    """m11-preview and m11-preview-deployer cannot read the bot token, create an unbounded role, or read the main
    state bucket; m11-preview-infra cannot update Cognito clients."""
    from module11.release import naming
    sm = ctx.admin.client("secretsmanager")
    token_arn = sm.describe_secret(SecretId=TOKEN_SECRET)["ARN"]
    for role in ("m11-preview", "m11-preview-infra", "m11-gitlab-ci"):
        assert _simulate(ctx, role, "secretsmanager:GetSecretValue", token_arn) != "allowed", role
    deployer = ctx.assume(naming.deployer_role("mr-0"), "mr-0", "live preview boundary")
    assert _denied(lambda: deployer.client("secretsmanager", region_name=ctx.region).get_secret_value(
        SecretId=TOKEN_SECRET)) in DENIED
    iam = deployer.client("iam", region_name=ctx.region)
    probe = f"m11-mr0-boundary-probe-{uuid.uuid4().hex[:6]}"
    trust = json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                                  "Principal": {"Service": "lambda.amazonaws.com"}}]})
    code = _denied(lambda: iam.create_role(RoleName=probe, AssumeRolePolicyDocument=trust,
                                           Tags=[{"Key": "Project", "Value": naming.PROJECT_TAG},
                                                 {"Key": "Stage", "Value": "mr-0"}]))
    if code == "allowed":                                    # never leave it behind
        ctx.admin.client("iam").delete_role(RoleName=probe)
    assert code in DENIED, f"CreateRole without the boundary returned {code}"
    main_state = naming.STATE_BUCKET.format(account=ctx.account, region=ctx.region)
    s3 = deployer.client("s3", region_name=ctx.region)
    assert _denied(lambda: s3.get_object(Bucket=main_state, Key=".pulumi/meta.yaml")) in DENIED | {"AccessDenied"}
    pool = __import__("module11.aws.params", fromlist=["shared_outputs"]).shared_outputs(
        ctx.admin.client("ssm"))["userPoolId"]
    assert _simulate(ctx, "m11-preview-infra", "cognito-idp:UpdateUserPoolClient",
                     _arn(ctx, "cognito-idp", f"userpool/{pool}")) != "allowed"


def test_live_preview_boundary_generated_ids(ctx):
    """m11-preview-infra: no PATCH on dev's REST API, no DeleteGuardrail on dev's guardrail (their real Stage
    tags passed as context); it may update its own mr-<a> stage's method and create a guardrail version there."""
    _need_mrs("M11_LIVE_MR_A")
    apigw, bedrock = ctx.admin.client("apigateway"), ctx.admin.client("bedrock")

    def ids(stage):
        dep = _current(ctx, stage)
        api = dep["apiUrl"].split("//", 1)[1].split(".", 1)[0]
        api_arn = f"arn:aws:apigateway:{ctx.region}::/restapis/{api}"
        guard_arn = _arn(ctx, "bedrock", f"guardrail/{dep['guardrail']['id']}")
        api_tag = apigw.get_tags(resourceArn=api_arn)["tags"].get("Stage")
        guard_tag = {t["key"]: t["value"] for t in bedrock.list_tags_for_resource(resourceARN=guard_arn)["tags"]}.get("Stage")
        return api_arn, guard_arn, api_tag, guard_tag

    api, guard, api_tag, guard_tag = ids("dev")
    assert _simulate(ctx, "m11-preview-infra", "apigateway:PATCH", f"{api}/resources/abc123",
                     {"aws:ResourceTag/Stage": api_tag}) != "allowed"
    assert _simulate(ctx, "m11-preview-infra", "bedrock:DeleteGuardrail", guard,
                     {"aws:ResourceTag/Stage": guard_tag}) != "allowed"
    mine = f"mr-{MR_A}"
    api, guard, api_tag, guard_tag = ids(mine)
    assert api_tag == mine and guard_tag == mine
    assert _simulate(ctx, "m11-preview-infra", "apigateway:PATCH", f"{api}/resources/abc123",
                     {"aws:ResourceTag/Stage": api_tag}) == "allowed"
    assert _simulate(ctx, "m11-preview-infra", "bedrock:CreateGuardrailVersion", guard,
                     {"aws:ResourceTag/Stage": guard_tag}) == "allowed"


# ---------------------------------------------------------------- end to end through the boundary
def test_live_preview_boundary_end_to_end(ctx):
    """An authenticated POST /invoke through the mr-<a> edge completes and GET /requests/{id} returns it; one
    Evaluate (Builtin.TrajectoryInOrderMatch) as the mr-<a> eval-runner role returns a value."""
    _need_mrs("M11_LIVE_MR_A")
    from test_module11_live_stage import _call, _token
    from module11 import cli_eval
    from module11.aws import evaluations as EV
    from module11.aws import spans as SP
    from module11.evaluation import runner as R
    stage = f"mr-{MR_A}"
    api = _current(ctx, stage)["apiUrl"].rstrip("/")
    token = _token(ctx, "platform")
    request_id = f"live-boundary-{uuid.uuid4().hex[:10]}"
    status, text = _call(f"{api}/invoke", "POST", token,
                         {"change_summary": "The checkout service is throwing 502s since the 14:20 deploy.",
                          "request_id": request_id})
    assert status == 200 and "event: result" in text, text[-300:]
    status, body = _call(f"{api}/requests/{request_id}", token=token)
    assert status == 200 and json.loads(body).get("status") == "completed", body[:300]

    d = cli_eval.definition(ROOT)
    target = cli_eval.target_of(ctx, stage)
    clients = cli_eval.clients_of(ctx, stage)                # assumes m11-mr-<a>-eval-runner (operator trust)
    specs = R.evaluator_specs(d["evaluators"], target.evaluator_ids)
    scenario = next(s for s in d["scenarios"] if s["scenario_id"] == "incident-triage-006")
    run = R.SuiteRun(clients, target, [scenario], repeats={"default": 1}, evaluators=specs, profile="live-test",
                     scope="full", evaluator_set_version=str(d["evaluators"]["version"]), suite=d["suite"],
                     kind="test", log=print)
    record = run.run_session(scenario, 1)
    assert record.status == "completed", (record.status, record.detail)
    _, missing = SP.wait_for_spans(
        lambda: SP.readiness_counts(clients.logs, target.log_group, f"m11-eval-{run.run_id}-", record.startedAt - 60,
                                    time.time()), [record.sessionId], last_invoke_at=record.endedAt, log=print)
    assert not missing
    spans = SP.session_spans(record.sessionId, target.log_group, record.startedAt, record.endedAt, logs=clients.logs)
    out = EV.evaluate(clients.dp, EV.request_for(
        "Builtin.TrajectoryInOrderMatch", spans, level="session",
        refs=EV.references("Builtin.TrajectoryInOrderMatch", scenario, record.sessionId)))
    assert out.ok and out.value is not None, out
    print("end to end:", json.dumps({"stage": stage, "requestId": request_id, "evaluate": out.value}))


# ---------------------------------------------------------------- cross-MR destroy and the stopping state
def _start_preview(ctx, action: str, iid: str, sha: str) -> dict:
    cb = ctx.admin.client("codebuild")
    build = cb.start_build(projectName="m11-preview", environmentVariablesOverride=[
        {"name": "PREVIEW_ACTION", "value": action, "type": "PLAINTEXT"},
        {"name": "PREVIEW_IID", "value": str(iid), "type": "PLAINTEXT"},
        {"name": "PREVIEW_SHA", "value": sha, "type": "PLAINTEXT"}])["build"]
    while True:
        time.sleep(10)
        build = cb.batch_get_builds(ids=[build["id"]])["builds"][0]
        if build["buildStatus"] != "IN_PROGRESS":
            return build


def _log_text(ctx, build: dict) -> str:
    logs = ctx.admin.client("logs")
    info = build.get("logs") or {}
    events = logs.get_log_events(logGroupName=info["groupName"], logStreamName=info["streamName"],
                                 startFromHead=True).get("events", [])
    return "\n".join(e["message"] for e in events)


def _gitlab(ctx):
    from module11.aws import gitlab_api as G
    return G.from_secret(ctx.admin.client("secretsmanager"), TOKEN_SECRET, os.environ["M11_GITLAB_PROJECT_ID"])


def test_live_preview_boundary_cross_mr_destroy(ctx):
    """A destroy of MR b's live environment is PreviewRejected and touches nothing; once b's environment is
    stopped through the API, its on_stop destroy succeeds (and records the state GitLab reports)."""
    _need_mrs("M11_LIVE_MR_A", "M11_LIVE_MR_B")
    gl = _gitlab(ctx)
    stage_b = f"mr-{MR_B}"
    mr_b = gl.merge_request(int(MR_B))
    assert mr_b["state"] == "opened"
    before = _current(ctx, stage_b)
    build = _start_preview(ctx, "destroy", MR_B, mr_b["sha"])
    text = _log_text(ctx, build)
    assert build["buildStatus"] == "FAILED" and "PreviewRejected" in text, text[-800:]
    assert _current(ctx, stage_b)["runtimeVersion"] == before["runtimeVersion"]
    ctl = ctx.admin.client("bedrock-agentcore-control")
    assert ctl.get_agent_runtime(agentRuntimeId=before["runtimeId"])["status"] == "READY"
    print("cross-MR destroy rejected:", build["id"])
    if os.environ.get("M11_LIVE_DESTROY_B") != "1":
        pytest.skip("set M11_LIVE_DESTROY_B=1 to stop MR b's environment and check its on_stop destroy")
    env = gl.environment(f"review/{stage_b}")
    assert env and env["state"] == "available"
    gl.stop_environment(int(env["id"]))
    seen, job_states, deadline = [], [], time.time() + 1500
    while time.time() < deadline:
        state = (gl.environment(f"review/{stage_b}") or {}).get("state")
        jobs = [j for j in gl.pipeline_jobs(gl.pipelines(sha=mr_b["sha"])[0]["id"]) if j["name"] == "preview:destroy"]
        job = jobs[0]["status"] if jobs else None
        seen.append(state)
        job_states.append(job)
        if job in ("success", "failed", "canceled") and state in ("stopped", "available"):
            break
        time.sleep(15)
    print("stopping state:", json.dumps({"environmentStates": sorted(set(s for s in seen if s)),
                                  "stoppingWhileJobRan": any(s == "stopping" and j in ("pending", "running")
                                                             for s, j in zip(seen, job_states)),
                                  "job": job_states[-1]}))
    assert job_states[-1] == "success"
    assert _denied(lambda: ctl.get_agent_runtime(agentRuntimeId=before["runtimeId"])) == "ResourceNotFoundException"
