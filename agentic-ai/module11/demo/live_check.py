# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Preflight for the Module 11 stages, following Module 9's CheckResult pattern:
masked values, a timeout per check, and remediation text. Exits 1 if any check is red.

Items (each needs the part of the deployment it checks):
  1  caller account and region match the environment
  1b IAM role headroom: the account's Roles-per-account quota (L-FE177D64) holds every role a full deployment
     creates (release/iam_budget.py: platform 8, 12 per stage, 13 per mr-<iid> stack); WARN under 10 spare
  4  per stage: endpoints READY and liveVersion equals the deployed manifest
  10 Cognito logins for the platform and payments users through Module 10's terminal client
  15 prod teamConcurrency (the live adapter's TEAM_CONCURRENCY) >= the largest traffic pool (12)
  16 generated-ID scope: the staging deployer is denied prod's REST API and guardrail (incl. retags), and
     every guardrail tagged Project=m11-devops-companion is a stage's deployed guardrail

    python -m module11.demo.live_check [--stage dev] [--preview-stacks N]
    python -m module11.demo.live_check --headroom      # before the first deployment: items 1 and 1b only
    python -m module11.demo.live_check --demo          # all 16 items, pre-approve state
    python -m module11.demo.live_check --demo --armed  # T-15: items 6/7 armed; writes demo-state.armed

--demo adds: 2 inference profiles ACTIVE; 3 Transaction Search; 5 drift clean on prod and staging;
6 prod IDLE, schedule DISABLED, staged digest not latched; 7 the newest m11-release execution waits at
Approval/ReleaseApproval with nothing behind it; 8 GitLab (API, merge settings, no m11-demo-run or
friday-brevity-dryrun-* MR; the demo MR open with preview:gate failed as designed; fallbackMr WARN); 9 Vercel;
11 captures b0-b7 and b5-canary; 12 no outcomes for the next rollout; 13 no Bedrock throttling in 15 min;
14 approve->open (WARN > 8:00, red > 9:00), detection and the v8 effect size; quota isolation (no
module11/.local/locks/*.lock). GitLab MR-flow and Vercel items are red with "set up per module11/PARTNER_SETUP.md" until then.
--armed: item 6 WARMING, 0 outcome items, schedule ENABLED, candidate READY on the release's runtime version,
m11.rollout.stalled expected; item 7 at Production with Deploy succeeded, Bake in progress and promote:production
succeeded. Its only write is demo-state.armed {rolloutId, pipelineExecutionId, tag, openedAt}, when 6 and 7 pass.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from module11.release import naming  # noqa: E402

LARGEST_POOL = 12
TIMEOUT = 90


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    return f"…{value[-4:]}" if len(value) > 4 else "*" * len(value)


def _timed(fn, label: str, seconds: int = TIMEOUT) -> list[CheckResult]:
    box: dict = {}

    def runner() -> None:
        try:
            box["result"] = fn()
        except Exception as exc:  # a failed call is a red check, never a traceback
            box["result"] = [CheckResult(label, False, f"{type(exc).__name__}: {str(exc)[:300]}")]
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return [CheckResult(label, False, f"timed out after {seconds} s; check AWS credentials and network")]
    return box.get("result", [CheckResult(label, False, "no result")])


def _session():
    import boto3
    return boto3.session.Session(region_name=os.environ.get("AWS_REGION") or "us-east-1")


def check_caller(session) -> list[CheckResult]:
    identity = session.client("sts").get_caller_identity()
    expected = os.environ.get("M11_EXPECTED_ACCOUNT", "")
    ok = not expected or identity["Account"] == expected
    region_ok = session.region_name == (os.environ.get("AWS_REGION") or session.region_name)
    return [CheckResult("1 caller account/region", ok and region_ok,
                        f"account {_mask(identity['Account'])} (expected {_mask(expected)}), region {session.region_name}"
                        + ("" if ok else "; fix: set AWS_PROFILE to the demo account"))]


def check_role_headroom(session, root: Path = ROOT, preview_stacks: int | None = None) -> list[CheckResult]:
    """Item 1b: roles still to be created by a full deployment fit under the account's quota."""
    from module11.aws import quotas
    from module11.release import iam_budget as IB
    stacks = IB.PREVIEW_STACKS if preview_stacks is None else preview_stacks
    room = quotas.headroom(session, IB.plan(root, preview_stacks=stacks))
    return [CheckResult("1b IAM role headroom", room.ok, room.message(), warning=room.warning)]


def check_endpoints(session, stages) -> list[CheckResult]:
    from module11.cli_live import stage_status

    class _Ctx:
        admin = session
    out = []
    for stage in stages:
        row = stage_status(_Ctx, stage)
        if row["state"] == "NOT DEPLOYED":
            out.append(CheckResult(f"4 {stage} endpoints", False, "not deployed; fix: module11 release deploy --stage "
                                   + stage))
            continue
        detail = ", ".join(f"{n} {e['status']} live v{e['liveVersion']} (deployed v{e['deployed']})"
                           for n, e in row["endpoints"].items())
        ok = row["state"] == "READY"
        if not ok and stage == "dev" and _dev_armed_ok(session, row):
            ok, detail = True, detail + "; candidate is the armed payments edit (demo-state.devArmed)"
        if not ok and stage == "prod" and _prod_armed_ok(session, row):
            ok, detail = True, (detail + "; candidate is the armed payments release, canary "
                                f"{row['rollout'].get('canaryPct', 0)}% (demo-state.armed)")
        out.append(CheckResult(f"4 {stage} endpoints", ok, f"{row['manifestVersion']}: {detail}"))
    return out


def _prod_armed_ok(session, row: dict) -> bool:
    """prod's candidate runs ahead of the deployed manifest on purpose while the payments release is armed (B5):
    `stage_status` reports CANARY (only the candidate ahead, on the open rollout's candidate version) and
    demo-state.armed names that rollout."""
    from module11.demo import state as S
    armed = S.read().get("armed") or {}
    rollout = row.get("rollout") or {}
    return row.get("state") == "CANARY" and bool(armed.get("rolloutId")) and \
        armed.get("rolloutId") == rollout.get("rolloutId")


def _dev_armed_ok(session, row: dict) -> bool:
    """dev's candidate runs ahead of the deployed manifest on purpose after `demo arm --stage dev` (Act 6 of the
    recording): every endpoint READY, only the candidate ahead, and demo-state.devArmed names the dev control row's
    open rollout (WARMING or OBSERVING)."""
    from module11.aws import control_table
    from module11.demo import state as S
    armed = S.read().get("devArmed") or {}
    endpoints = row.get("endpoints") or {}
    if not armed.get("rolloutId") or any(e["status"] != "READY" for e in endpoints.values()):
        return False
    ahead = [n for n, e in endpoints.items() if str(e["liveVersion"]) != str(e["deployed"])]
    if ahead != ["candidate"]:
        return False
    control = control_table.get(session.client("dynamodb"), naming.table("dev", "control")) or {}
    return control.get("rolloutId") == armed["rolloutId"] and control.get("state") in ("WARMING", "OBSERVING")


def check_logins(session) -> list[CheckResult]:
    path = os.environ.get("M11_MODULE10_CREDENTIALS_FILE")
    if not path or not Path(path).is_file():
        return [CheckResult("10 Cognito logins", False, "M11_MODULE10_CREDENTIALS_FILE is not set or missing")]
    creds = json.loads(Path(path).read_text(encoding="utf-8"))
    idp = session.client("cognito-idp")
    out = []
    for team in ("platform", "payments"):
        user = creds["users"][team]
        result = idp.initiate_auth(ClientId=creds["client_id"], AuthFlow="USER_PASSWORD_AUTH",
                                   AuthParameters={"USERNAME": user["username"], "PASSWORD": user["password"]})
        ok = bool(result.get("AuthenticationResult", {}).get("AccessToken"))
        out.append(CheckResult(f"10 Cognito login ({team})", ok, f"user {_mask(user['username'])}: "
                               + ("access token issued" if ok else "no token")))
    return out


def check_team_concurrency(session) -> list[CheckResult]:
    env = session.client("lambda").get_function_configuration(FunctionName=naming.function("prod", "adapter"))
    value = int(((env.get("Environment") or {}).get("Variables") or {}).get("TEAM_CONCURRENCY", "0"))
    return [CheckResult("15 prod teamConcurrency", value >= LARGEST_POOL,
                        f"{value} (demo value; default 10) >= largest traffic pool {LARGEST_POOL}")]


def _simulate(iam, role_arn, action, resource, context=None) -> str:
    entries = [{"ContextKeyName": k, "ContextKeyValues": v if isinstance(v, list) else [v],
                "ContextKeyType": "stringList" if isinstance(v, list) else "string"} for k, v in (context or {}).items()]
    result = iam.simulate_principal_policy(PolicySourceArn=role_arn, ActionNames=[action], ResourceArns=[resource],
                                           ContextEntries=entries)["EvaluationResults"][0]
    return result["EvalDecision"]


def check_generated_ids(session) -> list[CheckResult]:
    """Item 16, generated-ID scope and retags for m11-staging-deployer against prod; the
    real Stage tags are read and passed as context (the simulator does not read resource tags)."""
    from module11.aws import params
    ssm, iam, apigw, bedrock = (session.client(s) for s in ("ssm", "iam", "apigateway", "bedrock"))
    account, region = session.client("sts").get_caller_identity()["Account"], session.region_name
    prod, staging = (params.stage_get(ssm, s, "current") for s in ("prod", "staging"))
    if not prod or not staging:
        return [CheckResult("16 generated-ID scope", False, "prod and staging must be deployed")]
    role = f"arn:aws:iam::{account}:role/{naming.deployer_role('staging')}"
    prod_api = prod["deployment"]["prod"]["apiUrl"].split("//", 1)[1].split(".", 1)[0]
    staging_api = staging["deployment"]["staging"]["apiUrl"].split("//", 1)[1].split(".", 1)[0]
    api_arn = f"arn:aws:apigateway:{region}::/restapis/{prod_api}"
    api_tag = apigw.get_tags(resourceArn=api_arn)["tags"].get("Stage")
    guard = prod["deployment"]["prod"]["guardrail"]["id"]
    guard_arn = f"arn:aws:bedrock:{region}:{account}:guardrail/{guard}"
    guard_tag = {t["key"]: t["value"] for t in bedrock.list_tags_for_resource(resourceARN=guard_arn)["tags"]}.get("Stage")
    cases = [
        ("apigateway:PATCH", api_arn, {"aws:ResourceTag/Stage": api_tag}),
        ("apigateway:PATCH", f"{api_arn}/resources/abc123", {"aws:ResourceTag/Stage": api_tag}),
        ("apigateway:DELETE", api_arn, {"aws:ResourceTag/Stage": api_tag}),
        ("bedrock:DeleteGuardrail", guard_arn, {"aws:ResourceTag/Stage": guard_tag}),
        ("bedrock:UpdateGuardrail", guard_arn, {"aws:ResourceTag/Stage": guard_tag}),
        ("bedrock:TagResource", guard_arn, {"aws:ResourceTag/Stage": guard_tag, "aws:RequestTag/Stage": "staging",
                                            "aws:TagKeys": ["Stage"]}),
        ("apigateway:DELETE", f"arn:aws:apigateway:{region}::/tags/arn%3Aaws%3Aapigateway%3A{region}%3A%3A%2Frestapis%2F{staging_api}",
         {"aws:TagKeys": ["Stage"]}),
    ]
    out = []
    for action, resource, context in cases:
        decision = _simulate(iam, role, action, resource, context)
        # The simulator answers implicitDeny (no statement matched) for apigateway:DELETE on a /tags/ ARN,
        # where IAM itself returns AccessDenied (live probe); either is a denial.
        want = {"explicitDeny"} if action == "bedrock:TagResource" else {"explicitDeny", "implicitDeny"}
        out.append(CheckResult(f"16 staging deployer {action}", decision in want,
                               f"{resource.rsplit('/', 2)[-2:]} -> {decision} (want {'/'.join(sorted(want))})"))
    return out + check_preview_roles(session)


TOKEN_SECRET = "devops-companion/partners/gitlab-token"  # nosec B105 - a Secrets Manager secret name, not a secret value


def preview_role_cases(account: str, region: str, *, token_arn: str, pipeline_bucket: str, user_pool: str,
                       prod_api: str, prod_guardrail: str) -> list[tuple[str, str, str, dict, set[str]]]:
    """(role, action, resource, context, wanted decisions) for item 16's preview-role cases."""
    deny = {"explicitDeny", "implicitDeny"}
    cache = lambda project: f"arn:aws:s3:::{pipeline_bucket}/cache/{project}/x"  # noqa: E731
    api = f"arn:aws:apigateway:{region}::/restapis/{prod_api}"
    guard = f"arn:aws:bedrock:{region}:{account}:guardrail/{prod_guardrail}"
    cases = [(role, "secretsmanager:GetSecretValue", token_arn, {}, deny)
             for role in ("m11-preview", "m11-preview-infra", "m11-preview-image", "m11-gitlab-ci",
                          "m11-gitlab-ci-infra", "m11-gate", "m11-bake", "m11-seal-experiment")]
    cases += [("m11-seal", "secretsmanager:GetSecretValue", token_arn, {}, {"allowed"})]
    # Cache isolation (P10): MR-controlled builds touch no cache; trusted builds only their own prefix.
    cases += [(role, "s3:PutObject", cache("m11-deploy"), {}, deny) for role in ("m11-preview-image", "m11-preview-infra")]
    cases += [("m11-preview", "s3:PutObject", cache("m11-deploy"), {}, deny),
              ("m11-gate", "s3:PutObject", cache("m11-bake"), {}, deny),
              ("m11-deploy", "s3:PutObject", cache("m11-deploy"), {}, {"allowed"})]
    # m11-preview-infra: no Cognito writes, no generated-ID creates, no prod API or guardrail.
    pool = f"arn:aws:cognito-idp:{region}:{account}:userpool/{user_pool}"
    cases += [("m11-preview-infra", a, pool, {}, deny) for a in ("cognito-idp:UpdateUserPoolClient",
                                                                 "cognito-idp:CreateUserPoolClient")]
    cases += [("m11-preview-infra", "apigateway:POST", f"arn:aws:apigateway:{region}::/restapis",
               {"aws:RequestTag/Stage": "mr-1", "aws:RequestTag/Project": naming.PROJECT_TAG,
                "aws:TagKeys": ["Project", "Stage"]}, deny),
              ("m11-preview-infra", "bedrock:CreateGuardrail", "*",
               {"aws:RequestTag/Stage": "mr-1", "aws:RequestTag/Project": naming.PROJECT_TAG,
                "aws:TagKeys": ["Project", "Stage"]}, deny),
              ("m11-preview-infra", "apigateway:PATCH", api, {"aws:ResourceTag/Stage": "prod"}, deny),
              ("m11-preview-infra", "bedrock:DeleteGuardrail", guard, {"aws:ResourceTag/Stage": "prod"}, deny),
              ("m11-preview-infra", "bedrock:TagResource", guard,
               {"aws:ResourceTag/Stage": "prod", "aws:RequestTag/Stage": "mr-1", "aws:TagKeys": ["Stage"]}, deny)]
    # m11-gitlab-ci starts only m11-preview, and m11-operator-experiment only m11-seal-experiment (P11).
    project = lambda p: f"arn:aws:codebuild:{region}:{account}:project/{p}"  # noqa: E731
    cases += [("m11-gitlab-ci", "codebuild:StartBuild", project("m11-preview-infra"), {}, deny),
              ("m11-gitlab-ci", "codebuild:StartBuild", project("m11-seal-experiment"), {}, deny),
              ("m11-operator-experiment", "codebuild:StartBuild", project("m11-seal"), {}, deny),
              ("m11-preview", "codepipeline:PutApprovalResult",
               f"arn:aws:codepipeline:{region}:{account}:m11-release/Approval/ReleaseApproval", {}, deny)]
    return cases


def check_preview_roles(session) -> list[CheckResult]:
    """Item 16's preview-role cases (iam:SimulatePrincipalPolicy) and the OIDC audience (N12)."""
    from module11.aws import params
    iam, ssm, sm = session.client("iam"), session.client("ssm"), session.client("secretsmanager")
    account, region = session.client("sts").get_caller_identity()["Account"], session.region_name
    try:
        iam.get_role(RoleName="m11-preview")
    except Exception:  # noqa: BLE001 - before the pipeline exists
        return [CheckResult("16 preview-role and token cases", False, "m11-preview does not exist (enablePipeline)",
                            warning=True)]
    prod = params.stage_get(ssm, "prod", "current")
    dep = prod["deployment"]["prod"]
    token_arn = sm.describe_secret(SecretId=TOKEN_SECRET)["ARN"]
    shared = params.shared_outputs(ssm)
    cases = preview_role_cases(account, region, token_arn=token_arn, pipeline_bucket=shared["pipelineBucket"],
                               user_pool=shared["userPoolId"], prod_api=dep["apiUrl"].split("//", 1)[1].split(".", 1)[0],
                               prod_guardrail=dep["guardrail"]["id"])
    out, failed = [], []
    for role, action, resource, context, want in cases:
        decision = _simulate(iam, f"arn:aws:iam::{account}:role/{role}", action, resource, context)
        if decision not in want:
            failed.append(f"{role} {action} {resource.rsplit(':', 1)[-1]} -> {decision}")
    out.append(CheckResult("16 preview-role and token cases", not failed,
                           f"{len(cases) - len(failed)}/{len(cases)} as expected" + (f"; {failed}" if failed else "")))
    provider = f"arn:aws:iam::{account}:oidc-provider/gitlab.com"
    try:
        clients = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=provider)["ClientIDList"]
    except Exception as exc:  # noqa: BLE001
        return out + [CheckResult("16 OIDC audience", False, f"{provider}: {type(exc).__name__}")]
    audience = os.environ.get("M11_OIDC_AUD", "sts.amazonaws.com")
    out.append(CheckResult("16 OIDC audience", audience in clients, f"ClientIDList {clients} contains {audience}"))
    return out


def check_guardrail_ownership(session) -> list[CheckResult]:
    """Item 16: every guardrail tagged Project=m11-devops-companion is some stage's deployed guardrail."""
    from module11.aws import params
    ssm, bedrock = session.client("ssm"), session.client("bedrock")
    stages = list(naming.STAGES)
    paginator = ssm.get_paginator("describe_parameters")
    for page in paginator.paginate(ParameterFilters=[{"Key": "Name", "Option": "BeginsWith",
                                                      "Values": [f"{naming.PARAM_ROOT}/mr-"]}]):
        stages += sorted({p["Name"].split("/")[3] for p in page["Parameters"]})
    owned = {}
    for stage in stages:
        current = params.stage_get(ssm, stage, "current")
        if current:
            owned[current["deployment"][stage]["guardrail"]["id"]] = stage
    tagged, token = [], None
    while True:
        page = bedrock.list_guardrails(maxResults=100, **({"nextToken": token} if token else {}))
        for g in page.get("guardrails", []):
            tags = {t["key"]: t["value"] for t in bedrock.list_tags_for_resource(resourceARN=g["arn"])["tags"]}
            if tags.get("Project") == naming.PROJECT_TAG:
                tagged.append(g)
        token = page.get("nextToken")
        if not token:
            break
    orphans = [g["arn"] for g in tagged if g["id"] not in owned]
    return [CheckResult("16 no adopted guardrails", not orphans,
                        f"{len(tagged)} tagged guardrail(s), all deployed by {sorted(set(owned.values()))}" if not orphans
                        else f"not a stack output: {orphans}")]


# ---------------------------------------------------------------- --demo / --armed
PARTNER_TODO = "set up per module11/PARTNER_SETUP.md"
CAPTURE_BEATS = tuple(f"b{i}" for i in range(8)) + ("b5-canary",)
APPROVE_TO_OPEN_WARN_S = 8 * 60
APPROVE_TO_OPEN_RED_S = 9 * 60
NON_TERMINAL = ("WARMING", "OBSERVING", "ADVANCING", "ROLLING_BACK", "SHADOWING", "PROMOTING")


def item6(control: dict | None, *, schedule: str | None, latched: bool, outcomes: int, armed: bool,
          candidate: dict | None = None, candidate_version: int | None = None, stalled_ok: bool = True) -> CheckResult:
    """Item 6. --demo: IDLE (or terminal), schedule DISABLED, the staged digest not latched.
    --armed: WARMING, schedule ENABLED, 0 outcome items for the rollout, candidate READY on the release's runtime
    version, and an `m11.rollout.stalled` event is expected (it changes nothing)."""
    item = control or {}
    state = item.get("state", "IDLE")
    if not armed:
        ok = state in ("IDLE", "ROLLED_BACK", "PROMOTED") and schedule == "DISABLED" and not latched
        return CheckResult("6 prod rollout idle", ok,
                           f"state {state}, schedule {schedule}, staged digest {'LATCHED' if latched else 'not latched'}"
                           + ("" if ok else "; fix: module11 demo prep --reset (OPERATIONS.md)"))
    ready = bool(candidate) and candidate.get("status") == "READY" and candidate_version is not None and \
        str(candidate.get("liveVersion")) == str(candidate_version)
    ok = state == "WARMING" and schedule == "ENABLED" and outcomes == 0 and ready and stalled_ok
    detail = (f"state {state}, rollout {item.get('rolloutId')}, schedule {schedule}, {outcomes} outcome item(s), "
              f"candidate {(candidate or {}).get('status')} live v{(candidate or {}).get('liveVersion')} "
              f"(release runtime v{candidate_version})" + ("; m11.rollout.stalled is expected" if
                                                           item.get("stalled") else ""))
    if not ok:
        detail += ("; fix: wait for the Production stage to open the rollout (rollout opens <= 9:45 after the click), "
                   "else `module11 demo rearm` or the full reset cycle")
    return CheckResult("6 prod rollout armed", ok, detail)


def item7(executions: list[dict], rows: list[dict], *, staged_tag: str | None, armed: bool,
          promote_status: str | None = None, freeze: str = "none") -> tuple[CheckResult, dict]:
    """Item 7 over the newest m11-release execution and its action rows. --demo: waiting at
    Approval/ReleaseApproval for the staged tag, the newest execution, nothing queued behind it, freeze none.
    --armed: at Production with Deploy succeeded and Bake in progress, and promote:production succeeded.
    Returns (result, facts) where facts holds pipelineExecutionId and tag for the armed record."""
    from module11.demo import rearm as R
    if not executions:
        return CheckResult("7 staged release", False, "no m11-release execution; fix: demo prep --payments"), {}
    newest = executions[0]
    eid = newest["pipelineExecutionId"]
    tag = R.seal_tag(rows)
    facts = {"pipelineExecutionId": eid, "tag": tag}
    behind = [e for e in executions[1:] if e.get("status") in ("InProgress",)]
    latest = R._latest(rows, "Approval")
    approval = (latest.get("ReleaseApproval") or {}).get("status")
    if not armed:
        ok = (newest.get("status") == "InProgress" and approval == "InProgress" and not behind and freeze == "none"
              and (staged_tag is None or tag == staged_tag))
        detail = (f"newest execution {eid}: {newest.get('status')}, ReleaseApproval {approval}, tag {tag}, "
                  f"freeze {freeze}, {len(behind)} other in progress")
        if not ok:
            detail += "; fix: demo prep --payments, wait for the staging gate, then check the CodePipeline wait"
        return CheckResult("7 staged release waits at Approval", ok, detail), facts
    deploy = R.action_status(rows, "Deploy")
    bake = R.action_status(rows, "Bake")
    ok = deploy == "Succeeded" and bake == "InProgress" and promote_status == "success" and \
        (staged_tag is None or tag == staged_tag)
    detail = f"execution {eid} ({tag}): Production Deploy {deploy}, Bake {bake}, promote:production {promote_status}"
    if not ok:
        detail += "; fix: if promote:production failed on its 10-minute wait, run the full reset cycle and start later"
    return CheckResult("7 release at Production", ok, detail), facts


def captures_item(directory: Path) -> CheckResult:
    missing = [b for b in CAPTURE_BEATS if not (directory / f"{b}.json").is_file()]
    return CheckResult("11 replay captures", not missing,
                       f"{len(CAPTURE_BEATS) - len(missing)}/{len(CAPTURE_BEATS)} present"
                       + (f"; missing {missing}; fix: record them with `--record` in a rehearsal" if missing else ""))


def timings_item(*, approve_to_open_s: float | None, detection_s: float | None, effect_ok: bool | None,
                 mr_pipeline_s: float | None = None) -> CheckResult:
    """Item 14 (R-b, R-h): approve->open WARN above 8:00 and red above 9:00 (PutApprovalResult to m11-deploy
    success); detection (ROLLED_BACK <= t0 + 4:30) and the v8 effect size stay red conditions; the MR pipeline
    time is informational."""
    parts, red, warn = [], False, False
    if approve_to_open_s is None:
        parts.append("approve->open not measured yet")
        warn = True
    else:
        parts.append(f"approve->open {int(approve_to_open_s // 60)}:{int(approve_to_open_s % 60):02d}")
        red |= approve_to_open_s > APPROVE_TO_OPEN_RED_S
        warn |= approve_to_open_s > APPROVE_TO_OPEN_WARN_S
    if detection_s is None:
        parts.append("detection not measured yet")
        red = True
    else:
        parts.append(f"ROLLED_BACK at t0+{int(detection_s // 60)}:{int(detection_s % 60):02d}")
        red |= detection_s > 270
    if effect_ok is None or not effect_ok:
        parts.append("v8 effect size " + ("not measured" if effect_ok is None else "outside R10"))
        red = True
    else:
        parts.append("v8 effect size within R10")
    if mr_pipeline_s is not None:
        parts.append(f"MR pipeline {int(mr_pipeline_s // 60)}:{int(mr_pipeline_s % 60):02d} (informational)")
    detail = ", ".join(parts)
    if red:
        detail += "; fix: re-measure in a dry run (detection) or the pipeline timing levers (approve->open)"
    return CheckResult("14 rehearsal timings", not red, detail, warning=warn and not red)


def quota_item(locks: list, running: list[str]) -> CheckResult:
    names = [f"{lock.name}.lock ({lock.step})" for lock in locks]
    ok = not locks and not running
    return CheckResult("quota isolation", ok, ("no gate, calibration or traffic driver running and no "
                                               "module11/.local/locks/*.lock") if ok else
                       f"held: {names + running}; fix: wait for the prep chain step or stop it")


def record_armed(results: list[CheckResult], facts: dict, control: dict | None, state_path: Path | None = None) -> bool:
    """`--armed`'s only write: demo-state.armed when items 6 and 7 pass in armed form (through state.update)."""
    from module11.demo import state as S
    from module11.release.pins import ts
    six = [r for r in results if r.name == "6 prod rollout armed"]
    seven = [r for r in results if r.name == "7 release at Production"]
    if not (six and seven and all(r.ok for r in six + seven)) or not control:
        return False
    armed = {"rolloutId": control["rolloutId"], "pipelineExecutionId": facts["pipelineExecutionId"],
             "tag": facts["tag"], "openedAt": ts(control["openedAt"]).isoformat()}
    S.update(lambda doc: {**doc, "armed": armed}, state_path)
    return True


def _newest_measurement(local: Path, key: str):
    best = None
    for path in sorted((local / "timings").glob("*.json")) if (local / "timings").is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if doc.get(key) is not None:
            best = doc[key]
    return best


def demo_checks(session, *, armed: bool, root: Path = ROOT) -> tuple[list[CheckResult], dict]:
    """Items 2, 3, 5, 6, 7, 8, 9, 11, 12, 13, 14 and quota isolation (live reads only)."""
    from module11.aws import control_table, params
    from module11.demo import locks, rearm as R
    from module11.infrastructure.harness.operations import schedule_name
    out: list[CheckResult] = []
    local = root / "module11/.local"
    ssm, ddb = session.client("ssm"), session.client("dynamodb")
    facts: dict = {}

    def models() -> list[CheckResult]:
        import yaml
        doc = yaml.safe_load((root / "module11/harness/agent/models.yaml").read_text(encoding="utf-8")) or {}
        bedrock, runtime = session.client("bedrock"), session.client("bedrock-runtime")
        rows, seen = [], set()
        for role, m in sorted(doc.items()):
            mid = m.get("modelId") if isinstance(m, dict) else None
            if not mid or mid in seen:
                continue
            seen.add(mid)
            try:
                prof = bedrock.get_inference_profile(inferenceProfileIdentifier=mid)
                runtime.converse(modelId=mid, messages=[{"role": "user", "content": [{"text": "ping"}]}],
                                 inferenceConfig={"maxTokens": 16})       # the provider's minimum
                rows.append(CheckResult(f"2 model {mid}", prof.get("status") == "ACTIVE",
                                        f"{prof.get('status')}; one call completed ({role})"))
            except Exception as exc:  # noqa: BLE001
                rows.append(CheckResult(f"2 model {mid}", False, f"{type(exc).__name__}: {str(exc)[:160]}"))
        return rows or [CheckResult("2 models", False, "models.yaml lists no modelId")]
    out += _timed(models, "2 inference profiles")

    def tx_search() -> list[CheckResult]:
        status = session.client("xray").get_trace_segment_destination().get("Status")
        return [CheckResult("3 Transaction Search", status == "ACTIVE", f"trace segment destination {status}")]
    out += _timed(tx_search, "3 Transaction Search")

    def drift() -> list[CheckResult]:
        rows = []
        for stage in ("prod", "staging"):
            doc = params.get_json(ssm, f"{naming.PARAM_ROOT}/{stage}/drift") or {}
            findings = doc.get("findings") or []
            rows.append(CheckResult(f"5 drift {stage}", not findings,
                                    f"{len(findings)} finding(s) at {doc.get('checkedAt')}"
                                    + ("" if not findings else f"; fix: module11 drift revert --stage {stage}")))
        return rows
    out += _timed(drift, "5 drift")

    control = control_table.get(ddb, naming.table("prod", "control"))

    def six() -> list[CheckResult]:
        sched = session.client("scheduler").get_schedule(Name=schedule_name("prod"), GroupName="default")["State"]
        staged = (control or {}).get("behaviorDigest")
        latched = bool(staged) and ((params.latched(ssm, staged) or {}).get("state") == "latched")
        rid = (control or {}).get("rolloutId")
        outcomes = len(control_table.outcomes(ddb, naming.table("prod", "outcomes"), rid)) if rid else 0
        cand = None
        if armed and control:
            current = params.stage_get(ssm, "prod", "current") or {}
            runtime_id = current.get("deployment", {}).get("prod", {}).get("runtimeId")
            ep = session.client("bedrock-agentcore-control").get_agent_runtime_endpoint(
                agentRuntimeId=runtime_id, endpointName="candidate")
            cand = {"status": ep.get("status"), "liveVersion": ep.get("liveVersion")}
        return [item6(control, schedule=sched, latched=latched and not armed, outcomes=outcomes, armed=armed,
                      candidate=cand, candidate_version=(control or {}).get("candidateVersion"))]
    out += _timed(six, "6 prod rollout")

    def seven() -> list[CheckResult]:
        cp = session.client("codepipeline")
        execs = cp.list_pipeline_executions(pipelineName=R.PIPELINE, maxResults=10)["pipelineExecutionSummaries"]
        rows = R.actions(cp, execs[0]["pipelineExecutionId"]) if execs else []
        promote = None
        if armed:
            promote = _promote_status()
        result, f = item7(execs, rows, staged_tag=None, armed=armed, promote_status=promote,
                          freeze=params.freeze(ssm).get("state", "none"))
        facts.update(f)
        return [result]
    out += _timed(seven, "7 staged release")
    out += _timed(lambda: gitlab_items(session), "8 GitLab")
    out += _timed(lambda: vercel_items(session), "9 Vercel")
    out.append(captures_item(local / "captures"))
    if not armed:   # with --armed, item 6 requires 0 outcome items for the armed rollout
        out.append(CheckResult("12 outcomes for the next rollout", True,
                               "the next rollout ID is generated at open; the table holds none for it"))
    out += _timed(lambda: [bedrock_headroom(session)], "13 Bedrock headroom")
    out.append(timings_item(approve_to_open_s=_newest_measurement(local, "approveToOpenSeconds"),
                            detection_s=_newest_measurement(local, "rolledBackAfterSeconds"),
                            effect_ok=_newest_measurement(local, "effectSizeOk")))
    out.append(quota_item(locks.held(), running_jobs()))
    return out, {**facts, "control": control}


QUIET_JOBS = ("gate", "calibrate", "evalset", "traffic", "abtest", "model", "shadow", "recommendation")


def running_jobs(ps_output: str | None = None) -> list[str]:
    """Local gate, calibration, evaluator-set, traffic, A/B, model-compare, shadow and recommendation commands
    (quota isolation from T-30); the prep chain itself counts too."""
    if ps_output is None:
        import subprocess
        ps_output = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    found = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, command = parts
        words = command.split()
        if "prep-chain.sh" in command and "caffeinate" not in command:
            found.append(f"prep-chain (pid {pid})")
        elif "module11" in words and "-m" in words:
            after = words[words.index("module11") + 1:]
            if after[:1] == ["--root"]:
                after = after[2:]
            if after and after[0] in QUIET_JOBS:
                found.append(f"module11 {' '.join(after[:2])} (pid {pid})")
    return found


def _promote_status() -> str | None:
    """Status of the newest promote:production job on main (GitLab), or None when it cannot be read."""
    try:
        gl = _gitlab_client()
        for pipe in gl.pipelines(ref="main")[:5]:
            for job in gl.pipeline_jobs(pipe["id"]):
                if job.get("name") == "promote:production":
                    return job.get("status")
    except Exception:  # noqa: BLE001
        return None
    return None


def _gitlab_client():
    from module11.aws import gitlab_api as G
    session = _session()
    return G.from_secret(session.client("secretsmanager"), os.environ.get("M11_GITLAB_TOKEN_SECRET", TOKEN_SECRET),
                         os.environ.get("M11_GITLAB_PROJECT_ID", ""))


def gitlab_items(session) -> list[CheckResult]:
    """Item 8. The API and project settings are checked now; the MR flow (demo MR, its red gate and note, the
    fallback MR, promote:production waiting) is red with 'set up per module11/PARTNER_SETUP.md' until the partner setup is done."""
    from module11.demo import state as S
    gl = _gitlab_client()
    user = gl.current_user()
    project = gl.project()
    out = [CheckResult("8 GitLab API", bool(user.get("id")), f"GET /user 200 ({_mask(str(user.get('username', '')))})")]
    settings_ok = project.get("only_allow_merge_if_pipeline_succeeds") is True and \
        project.get("allow_merge_on_skipped_pipeline") is False
    out.append(CheckResult("8 GitLab merge settings", settings_ok,
                           f"only_allow_merge_if_pipeline_succeeds {project.get('only_allow_merge_if_pipeline_succeeds')}, "
                           f"allow_merge_on_skipped_pipeline {project.get('allow_merge_on_skipped_pipeline')}"
                           + ("" if settings_ok else f"; {PARTNER_TODO} (module11 partners up)")))
    out.append(governance_item(gl, project))
    open_mrs = gl.merge_requests(state="opened")
    demo_run = [m["iid"] for m in open_mrs if "m11-demo-run" in (m.get("labels") or [])]
    out.append(CheckResult("8 no open m11-demo-run MR", not demo_run,
                           "none" if not demo_run else f"open: {demo_run}; fix: demo prep --reset (step 9b)"))
    dry = [m["iid"] for m in open_mrs if str(m.get("source_branch", "")).startswith("friday-brevity-dryrun-")]
    out.append(CheckResult("8 no friday-brevity-dryrun-* MR", not dry, "none" if not dry else f"open: {dry}"))
    friday = [m for m in open_mrs if m.get("source_branch") == "friday-brevity"]
    if not friday:
        out.append(CheckResult("8 demo MR (friday-brevity)", False, f"no open friday-brevity MR; {PARTNER_TODO} "
                               "(the MR flow), then push the Friday commit on Monday"))
    else:
        mr = gl.merge_request(int(friday[0]["iid"]))     # the list endpoint carries no head_pipeline
        pipe = mr.get("head_pipeline") or {}
        jobs = {j["name"]: j["status"] for j in gl.pipeline_jobs(pipe["id"])} if pipe.get("id") else {}
        ok = jobs.get("validate") == "success" and jobs.get("preview:deploy") == "success" and \
            jobs.get("preview:gate") == "failed"
        out.append(CheckResult("8 demo MR (friday-brevity)", ok, f"!{mr['iid']}: validate {jobs.get('validate')}, "
                               f"preview:deploy {jobs.get('preview:deploy')}, preview:gate {jobs.get('preview:gate')} "
                               "(expected: failed, as designed)"))
        owners = [r for r in gl.approval_state(int(mr["iid"])) if r["type"] == "code_owner"]
        behavior = [r for r in owners if r.get("section") == "Behavior"]
        out.append(CheckResult("8 demo MR code owners", bool(behavior),
                               ", ".join(f"[{r['section']}] {r['name']} {'approved' if r['approved'] else 'required'}"
                                         for r in owners) or "no code-owner rule on the MR (3d shows the widget)"))
    fallback = S.read().get("fallbackMr")
    out.append(CheckResult("8 fallback MR", bool(fallback), f"fallbackMr {fallback or 'not recorded'}"
                           + ("" if fallback else "; B3 falls back to the replay"), warning=not fallback))
    return out


def governance_ok(main: dict | None, push_rule: dict, project: dict) -> tuple[bool, str]:
    """Item 8, the MR governance `partners up` applies: nobody pushes to main, code owners approve, unsigned pushes
    are rejected, and the bot's API commits are signed by GitLab (web-based commit signing)."""
    push = sorted((a.get("access_level_description") or str(a.get("access_level")))
                  for a in (main or {}).get("push_access_levels") or [])
    checks = {"main protected": bool(main),
              "no direct push": bool(main) and all(a.get("access_level") == 0
                                                   for a in main.get("push_access_levels") or [{}]),
              "code-owner approval": bool((main or {}).get("code_owner_approval_required")),
              "no force push": not (main or {}).get("allow_force_push"),
              "reject unsigned": bool(push_rule.get("reject_unsigned_commits")),
              "web-based signing": bool(project.get("web_based_commit_signing_enabled"))}
    missing = [k for k, v in checks.items() if not v]
    return not missing, (f"main push {push or 'unprotected'}; " +
                         ("all set" if not missing else "missing: " + ", ".join(missing)))


def governance_item(gl, project: dict) -> CheckResult:
    ok, detail = governance_ok(gl.protected_branch("main"), gl.push_rule(), project)
    return CheckResult("8 GitLab MR governance", ok, detail + ("" if ok else "; fix: module11 partners up"))


def vercel_items(session) -> list[CheckResult]:
    sm = session.client("secretsmanager")
    try:
        sm.describe_secret(SecretId=os.environ.get("M11_VERCEL_TOKEN_SECRET", "devops-companion/partners/vercel-token"))
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("9 Vercel token", False, f"{type(exc).__name__}; {PARTNER_TODO}")]
    state = __import__("module11.demo.state", fromlist=["read"]).read()
    preview = state.get("vercelPreview")
    return [CheckResult("9 Vercel token", True, "secret present"),
            CheckResult("9 Vercel MR preview and alias", bool(preview),
                        f"{preview}" if preview else f"no MR preview recorded; {PARTNER_TODO}")]


def bedrock_headroom(session) -> CheckResult:
    from datetime import datetime, timedelta, timezone
    cw = session.client("cloudwatch")
    end = datetime.now(timezone.utc)
    stats = cw.get_metric_statistics(Namespace="AWS/Bedrock", MetricName="InvocationThrottles",
                                     StartTime=end - timedelta(minutes=15), EndTime=end, Period=900, Statistics=["Sum"])
    total = sum(p.get("Sum", 0) for p in stats.get("Datapoints") or [])
    return CheckResult("13 Bedrock headroom", total == 0, f"{int(total)} throttled invocation(s) in the last 15 min"
                       + ("" if total == 0 else "; fix: wait for quiet, or stop the running gate"))


def run(stages: list[str], preview_stacks: int | None = None) -> list[CheckResult]:
    session = _session()
    results = []
    results += _timed(lambda: check_caller(session), "1 caller")
    results += _timed(lambda: check_role_headroom(session, preview_stacks=preview_stacks), "1b IAM role headroom")
    results += _timed(lambda: check_endpoints(session, stages), "4 endpoints", 180)
    results += _timed(lambda: check_logins(session), "10 Cognito logins")
    results += _timed(lambda: check_team_concurrency(session), "15 prod teamConcurrency")
    results += _timed(lambda: check_generated_ids(session), "16 generated-ID scope")
    results += _timed(lambda: check_guardrail_ownership(session), "16 guardrail ownership")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stage", help="check one stage's endpoints (default: dev, staging, prod)")
    parser.add_argument("--preview-stacks", type=int, default=None,
                        help="concurrent mr-<iid> stacks to budget roles for (default 2: the Friday MR and fallbackMr)")
    parser.add_argument("--headroom", action="store_true",
                        help="before the first deployment: run only items 1 and 1b (account and IAM role headroom)")
    parser.add_argument("--demo", action="store_true", help="all 16 items in the pre-approve form")
    parser.add_argument("--armed", action="store_true",
                        help="with --demo: items 6 and 7 in armed form; writes demo-state.armed when both pass")
    args = parser.parse_args(argv)
    if args.armed and not args.demo:
        parser.error("--armed needs --demo")
    if args.demo:
        session = _session()
        results = run(list(naming.STAGES), preview_stacks=args.preview_stacks)
        extra, facts = demo_checks(session, armed=args.armed)
        results += extra
        if args.armed:
            if record_armed(results, facts, facts.get("control")):
                print(f"armed: demo-state.armed = rollout {facts['control']['rolloutId']}, execution "
                      f"{facts['pipelineExecutionId']}, {facts['tag']}")
            else:
                print("not armed: items 6 and 7 must pass in armed form; demo-state.armed unchanged")
    elif args.headroom:
        session = _session()
        results = _timed(lambda: check_caller(session), "1 caller") + _timed(
            lambda: check_role_headroom(session, preview_stacks=args.preview_stacks), "1b IAM role headroom")
    else:
        results = run([args.stage] if args.stage else list(naming.STAGES), preview_stacks=args.preview_stacks)
    for r in results:
        mark = "WARN" if r.warning else ("ok  " if r.ok else "FAIL")
        print(f"{mark} {r.name}: {r.detail}")
    red = [r for r in results if not r.ok and not r.warning]
    print(f"{len(results) - len(red)}/{len(results)} checks green")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
