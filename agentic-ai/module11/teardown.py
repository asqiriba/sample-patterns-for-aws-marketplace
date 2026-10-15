# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 teardown --dry-run | --yes [--include-orphans] [--include-module10] [--delete-gitlab-project]`.

Ownership rule: a resource is deleted only if (1) it is tagged Project=m11-devops-companion, (2) its URN or
physical ID is in an m11-* Pulumi stack state, or (3) it is recorded in an entry object under
s3://<release-bucket>/ledger/. The ledger is read ONCE, before step 1, with paged ListObjectsV2(Prefix="ledger/")
(every ledger/<stage>/ and ledger/shared/), and the whole plan is built from it, so ledgered resources stay known
after step 5 destroys the release bucket. A resource matching only the `m11` name prefix is listed `ORPHAN?` and
kept; deleting orphans needs --include-orphans plus the typed confirmation `delete <n> orphans`. Runtime log groups
are matched by runtime IDs from stack state, and online-evaluation result groups by config IDs from stack state,
never by name prefix. Module 10's stacks share the state backend and are touched only with --include-module10.
The plan (steps 1-10) is printed before anything is deleted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

PROJECT_TAG = "m11-devops-companion"
STAGE_STACKS = ("prod", "staging", "dev")
OWNED_PROJECTS = ("m11-harness", "m11-platform", "m11-partners")
MODULE10_PROJECTS = ("module10-exposure", "module10-portkey")
RUNTIME_TYPE = "aws:bedrock/agentcoreAgentRuntime:AgentcoreAgentRuntime"
ONLINE_EVAL_TYPE = "aws:bedrock/agentcoreOnlineEvaluationConfig:AgentcoreOnlineEvaluationConfig"
LAMBDA_TYPE = "aws:lambda/function:Function"
CODEBUILD_TYPE = "aws:codebuild/project:Project"


@dataclass
class Item:
    step: int
    kind: str
    ident: str
    why: str                       # the ownership rule that admits it: tag | state | ledger
    detail: dict = field(default_factory=dict)

    def line(self) -> str:
        return f"  {self.kind:<22} {self.ident}  [{self.why}]"


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    orphans: list[Item] = field(default_factory=list)
    stacks: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def step(self, n: int) -> list[Item]:
        return [i for i in self.items if i.step == n]


STEP_TITLES = {
    1: "stop any active rollout (controller abort, wait for ROLLED_BACK) and disable the controller schedules",
    2: "stop and delete A/B tests; delete batch evaluations, recommendations, Gateway trace deliveries and latch "
       "parameters listed in the ledger",
    3: "pulumi destroy every mr-* stack, then stack rm",
    4: "pulumi destroy prod, staging and dev (guardrails take every version with them; ledgered bundle versions by ID)",
    5: "pulumi destroy m11-platform (pipeline, CodeBuild, connection, OIDC provider if tagged ours, EventBridge, "
       "release bucket)",
    6: "pulumi destroy m11-partners (Vercel project, GitLab protections, notes token); remove the bot's signing key; "
       "archive the GitLab project (or delete it with --delete-gitlab-project)",
    7: "delete runtime and online-evaluation result log groups by IDs from stack state, and Lambda/CodeBuild groups "
       "named in a destroyed stack's state",
    8: "Module 10 (only with --include-module10; see module10/OPERATIONS.md)",
    9: "bootstrap last: both state buckets (main and preview; export state first if wanted), then both KMS keys "
       "(scheduled deletion, 7 days)",
    10: "revoke the partner secrets (partner guide section 5.4): the GitLab token and the Vercel token",
}


# ---------------------------------------------------------------- reads
def read_ledger(s3: Any, bucket: str, *, page_size: int = 1000) -> list[dict]:
    """Every ledger entry, from one paged ListObjectsV2(Prefix='ledger/') listing."""
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": "ledger/", "MaxKeys": page_size}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents") or []]
        token = page.get("NextContinuationToken")
        if not token:
            break
    out = []
    for key in keys:
        doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        out.append({**doc, "_key": key})
    return out


def stack_resources(state_doc: Mapping) -> list[dict]:
    return list(((state_doc.get("checkpoint") or {}).get("latest") or {}).get("resources") or [])


def read_states(s3: Any, bucket: str, *, projects: Iterable[str] = OWNED_PROJECTS) -> dict[str, list[dict]]:
    """{"<project>/<stack>": resources} from the DIY backend's .pulumi/stacks/<project>/<stack>.json."""
    out = {}
    for project in projects:
        token = None
        while True:
            kw = {"Bucket": bucket, "Prefix": f".pulumi/stacks/{project}/"}
            if token:
                kw["ContinuationToken"] = token
            page = s3.list_objects_v2(**kw)
            for obj in page.get("Contents") or []:
                if not obj["Key"].endswith(".json"):
                    continue
                doc = json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
                out[f"{project}/{Path(obj['Key']).stem}"] = stack_resources(doc)
            token = page.get("NextContinuationToken")
            if not token:
                break
    return out


# ---------------------------------------------------------------- the plan (pure)
def _outputs(r: Mapping) -> dict:
    return r.get("outputs") or {}


def state_ids(states: Mapping[str, list[dict]]) -> dict[str, set[str]]:
    """Physical IDs, runtime IDs, online-eval config IDs, Lambda and CodeBuild names found in stack state."""
    ids = {"physical": set(), "runtime": set(), "onlineEval": set(), "lambda": set(), "codebuild": set(),
           "arn": set()}
    for resources in states.values():
        for r in resources:
            out = _outputs(r)
            if r.get("id"):
                ids["physical"].add(str(r["id"]))
            if out.get("arn"):
                ids["arn"].add(str(out["arn"]))
            if r.get("type") == RUNTIME_TYPE and out.get("agentRuntimeId"):
                ids["runtime"].add(out["agentRuntimeId"])
            if r.get("type") == ONLINE_EVAL_TYPE and out.get("onlineEvaluationConfigId"):
                ids["onlineEval"].add(out["onlineEvaluationConfigId"])
            if r.get("type") == LAMBDA_TYPE and out.get("name"):
                ids["lambda"].add(out["name"])
            if r.get("type") == CODEBUILD_TYPE and out.get("name"):
                ids["codebuild"].add(out["name"])
    return ids


def build_plan(*, ledger: list[dict], states: Mapping[str, list[dict]], preview_states: Mapping[str, list[dict]],
               tagged: Iterable[str], prefixed: Iterable[tuple[str, str]], log_groups: Iterable[str],
               include_module10: bool = False, delete_gitlab_project: bool = False) -> Plan:
    """The full plan from what was read before step 1. `prefixed` = (kind, identifier) of resources whose name
    starts with m11; `log_groups` = every log group name that may be ours."""
    plan = Plan()
    ids = state_ids({**states, **preview_states})
    tagged = set(tagged)
    for stage in ("prod", "dev"):
        plan.items.append(Item(1, "rollout", stage, "state", {"stage": stage}))
    for stage in STAGE_STACKS:
        plan.items.append(Item(1, "controller schedule", f"m11-{stage}-controller", "state", {"stage": stage}))
    seen_latch = set()
    for e in ledger:
        kind = e.get("kind")
        if kind == "abtest" and e.get("abTestId"):
            plan.items.append(Item(2, "A/B test", e["abTestId"], "ledger", {"key": e["_key"], "name": e.get("name")}))
        elif kind == "batch-evaluation" and e.get("batchEvaluationId"):
            plan.items.append(Item(2, "batch evaluation", e["batchEvaluationId"], "ledger", {"key": e["_key"]}))
        elif kind == "recommendation" and e.get("recommendationId"):
            plan.items.append(Item(2, "recommendation", e["recommendationId"], "ledger", {"key": e["_key"]}))
        elif kind == "gateway-tracing" and e.get("deliveryId"):
            plan.items.append(Item(2, "trace delivery", e["deliveryId"], "ledger",
                                   {"source": e.get("deliverySource"), "destination": e.get("deliveryDestination")}))
        elif kind in ("latch", "latch-clear") and e.get("parameter") and e["parameter"] not in seen_latch:
            seen_latch.add(e["parameter"])
            plan.items.append(Item(2, "latch parameter", e["parameter"], "ledger", {"key": e["_key"]}))
        elif kind == "bundle-version" and e.get("versionId") and e.get("purpose"):
            plan.items.append(Item(4, "bundle version", f"{e.get('bundleArn')}:{e['versionId']}", "ledger",
                                   {"key": e["_key"]}))
        elif kind == "bot-signing-key" and e.get("keyId"):
            plan.items.append(Item(6, "bot signing key", str(e["keyId"]), "ledger", {"key": e["_key"]}))
    for name in sorted(preview_states):
        if name.startswith("m11-harness/mr-"):
            plan.items.append(Item(3, "stack", name, "state", {"resources": len(preview_states[name])}))
    for stage in STAGE_STACKS:
        name = f"m11-harness/{stage}"
        if name in states:
            plan.items.append(Item(4, "stack", name, "state", {"resources": len(states[name])}))
    if "m11-platform/shared" in states:
        plan.items.append(Item(5, "stack", "m11-platform/shared", "state",
                               {"resources": len(states["m11-platform/shared"])}))
    if "m11-partners/shared" in states:
        plan.items.append(Item(6, "stack", "m11-partners/shared", "state",
                               {"resources": len(states["m11-partners/shared"])}))
    plan.items.append(Item(6, "GitLab project", "delete" if delete_gitlab_project else "archive", "state"))
    groups = set(log_groups)
    for g in sorted(groups):
        runtime = g.removeprefix("/aws/bedrock-agentcore/runtimes/").rsplit("-", 1)[0] \
            if g.startswith("/aws/bedrock-agentcore/runtimes/") else None
        config = g.removeprefix("/aws/bedrock-agentcore/evaluations/results/") \
            if g.startswith("/aws/bedrock-agentcore/evaluations/results/") else None
        fn = g.removeprefix("/aws/lambda/") if g.startswith("/aws/lambda/") else None
        cb = g.removeprefix("/aws/codebuild/") if g.startswith("/aws/codebuild/") else None
        if (runtime and runtime in ids["runtime"]) or (config and config in ids["onlineEval"]) or \
                (fn and fn in ids["lambda"]) or (cb and cb in ids["codebuild"]):
            plan.items.append(Item(7, "log group", g, "state"))
        elif "m11" in g:
            plan.orphans.append(Item(7, "log group", g, "ORPHAN?"))
    if include_module10:
        for p in MODULE10_PROJECTS:
            plan.items.append(Item(8, "Module 10 stacks", p, "explicit --include-module10"))
    else:
        plan.notes.append("Module 10 is kept (pass --include-module10 to tear it down too)")
    plan.items += [Item(9, "state bucket", "m11-pulumi-state-<account>-<region>", "tag"),
                   Item(9, "state bucket", "m11-pulumi-state-preview-<account>-<region>", "tag"),
                   Item(9, "KMS key", "alias/m11-pulumi-secrets and alias/m11-pulumi-secrets-preview (7-day)", "tag"),
                   Item(10, "partner secret", "devops-companion/partners/gitlab-token (revoke in GitLab first)",
                        "manual"),
                   Item(10, "partner secret", "devops-companion/partners/vercel-token (revoke in Vercel first)",
                        "manual")]
    owned = ids["physical"] | ids["arn"] | ids["runtime"] | ids["lambda"] | ids["codebuild"] | tagged
    for kind, ident in prefixed:
        if ident in owned or any(ident.endswith(f"/{x}") or ident.endswith(f":{x}") for x in ids["lambda"]):
            continue
        plan.orphans.append(Item(0, kind, ident, "ORPHAN?"))
    return plan


def render(plan: Plan, say: Callable[[str, str | None], None]) -> None:
    say("module11 teardown plan (nothing is deleted before this list is printed)", "bold")
    for n in range(1, 11):
        say(f"step {n}: {STEP_TITLES[n]}", "bold")
        for item in plan.step(n):
            say(item.line(), None)
    if plan.orphans:
        say(f"ORPHAN? ({len(plan.orphans)}): match only the m11 name prefix; kept unless --include-orphans and the "
            f"typed confirmation 'delete {len(plan.orphans)} orphans'", "yellow")
        for item in plan.orphans:
            say(item.line(), "yellow")
    else:
        say("ORPHAN?: none", "green")
    for note in plan.notes:
        say(note, "dim")


# ---------------------------------------------------------------- live
def _prefixed(session: Any) -> list[tuple[str, str]]:
    """Resources whose name starts with m11 (candidates; ownership decides)."""
    out: list[tuple[str, str]] = []
    ctl = session.client("bedrock-agentcore-control")
    token = None
    while True:
        page = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for r in page.get("agentRuntimes") or []:
            if str(r.get("agentRuntimeName", "")).startswith("m11"):
                out.append(("runtime", r["agentRuntimeId"]))
        token = page.get("nextToken")
        if not token:
            break
    lam = session.client("lambda")
    for page in lam.get_paginator("list_functions").paginate():
        out += [("lambda", f["FunctionName"]) for f in page["Functions"] if f["FunctionName"].startswith("m11-")]
    iam = session.client("iam")
    for page in iam.get_paginator("list_roles").paginate():
        out += [("role", r["Arn"]) for r in page["Roles"] if r["RoleName"].startswith("m11-")]
    return out


def _log_groups(session: Any) -> list[str]:
    logs = session.client("logs")
    names = []
    for prefix in ("/aws/bedrock-agentcore/runtimes/", "/aws/bedrock-agentcore/evaluations/results/",
                   "/aws/lambda/m11-", "/aws/codebuild/m11-"):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            names += [g["logGroupName"] for g in page["logGroups"]]
    return names


def _tagged(session: Any) -> list[str]:
    tagging = session.client("resourcegroupstaggingapi")
    arns = []
    for page in tagging.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Project",
                                                                             "Values": [PROJECT_TAG]}]):
        arns += [r["ResourceARN"] for r in page["ResourceTagMappingList"]]
    return arns


def plan_live(ctx, *, include_module10: bool = False, delete_gitlab_project: bool = False) -> Plan:
    from module11.release import naming
    s3 = ctx.admin.client("s3")
    ledger = read_ledger(s3, ctx.release_bucket)                      # once, before step 1
    main = naming.STATE_BUCKET.format(account=ctx.account, region=ctx.region)
    preview = naming.PREVIEW_STATE_BUCKET.format(account=ctx.account, region=ctx.region)
    states = read_states(s3, main)
    try:
        preview_states = read_states(s3, preview, projects=("m11-harness",))
    except Exception:  # noqa: BLE001 - no preview backend yet
        preview_states = {}
    plan = build_plan(ledger=ledger, states=states, preview_states=preview_states, tagged=_tagged(ctx.admin),
                      prefixed=_prefixed(ctx.admin), log_groups=_log_groups(ctx.admin),
                      include_module10=include_module10, delete_gitlab_project=delete_gitlab_project)
    plan.notes.append(f"ledger: {len(ledger)} entries read once; stacks in state: {', '.join(sorted(states))}")
    return plan


def execute(ctx, plan: Plan, *, include_orphans: bool, say: Callable[[str, str | None], None],
            confirm: Callable[[str], str] = input) -> int:  # pragma: no cover - destructive; exercised by hand only
    """Run the printed plan in order. Each step reports what it did; a failure stops the teardown there."""
    from module11.aws import params
    root = ctx.root
    py = [sys.executable, "-m", "module11"]
    for stage in ("prod", "dev"):
        item = params.stage_get(ctx.admin.client("ssm"), stage, "current")
        if item:
            # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI with a fixed stage (prod or dev)
            subprocess.run(py + ["rollout", "abort", "--stage", stage, "--reason", "teardown"], cwd=root)
    sched = ctx.admin.client("scheduler")
    for item in plan.step(1):
        if item.kind == "controller schedule":
            try:
                s = sched.get_schedule(Name=item.ident, GroupName="default")
                keep = {k: s[k] for k in ("Name", "GroupName", "ScheduleExpression", "FlexibleTimeWindow", "Target")
                        if k in s}
                sched.update_schedule(**keep, State="DISABLED")
            except Exception as exc:  # noqa: BLE001
                say(f"  schedule {item.ident}: {type(exc).__name__}", "yellow")
    ctl = ctx.admin.client("bedrock-agentcore-control")
    for item in plan.step(2):
        try:
            if item.kind == "A/B test":
                ctl.stop_ab_test(abTestId=item.ident)
                ctl.delete_ab_test(abTestId=item.ident)
            elif item.kind == "batch evaluation":
                ctl.delete_batch_evaluation(batchEvaluationId=item.ident)
            elif item.kind == "recommendation":
                ctl.delete_recommendation(recommendationId=item.ident)
            elif item.kind == "trace delivery":
                ctx.admin.client("logs").delete_delivery(id=item.ident)
            elif item.kind == "latch parameter":
                ctx.admin.client("ssm").delete_parameter(Name=item.ident)
            say(f"  deleted {item.kind} {item.ident}", None)
        except Exception as exc:  # noqa: BLE001 - already gone counts as done
            say(f"  {item.kind} {item.ident}: {type(exc).__name__}", "yellow")
    for item in plan.step(3) + plan.step(4):
        if item.kind != "stack":
            continue
        stage = item.ident.split("/", 1)[1]
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI; the stage is from a planned m11-harness/<stage> stack
        code = subprocess.run(py + ["release", "destroy", "--stage", stage, "--confirm-stage", stage], cwd=root).returncode
        if code:
            say(f"step stopped: release destroy {stage} exited {code}", "red")
            return code
    for step, project_dir in ((5, "module11/infrastructure/platform"), (6, "module11/infrastructure/partners")):
        for item in plan.step(step):
            if item.kind != "stack":
                continue
            env = {**os.environ, "PULUMI_BACKEND_URL": ctx.backend("prod")}
            stack = f"organization/{item.ident.split('/')[0]}/shared"
            code = subprocess.run(["pulumi", "destroy", "--stack", stack, "--yes", "--non-interactive"],
                                  cwd=root / project_dir, env=env).returncode
            if code:
                say(f"step stopped: pulumi destroy {stack} exited {code}", "red")
                return code
    gl = None
    for item in plan.step(6):
        if item.kind not in ("bot signing key", "GitLab project"):
            continue
        if gl is None:
            from module11.aws import gitlab_api as G
            gl = G.from_secret(ctx.admin.client("secretsmanager"),
                               os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"),
                               os.environ.get("M11_GITLAB_PROJECT_ID", ""))
        if item.kind == "bot signing key":            # by its ledger/shared entry's key ID (planned before step 5)
            gl.delete(f"/user/keys/{item.ident}")
            ctx.admin.client("secretsmanager").delete_secret(
                SecretId="devops-companion/partners/gitlab-bot-signing-key", RecoveryWindowInDays=7)
        elif item.ident == "delete":
            gl.delete(gl.p)
        else:
            gl.post(f"{gl.p}/archive")
        say(f"  {item.kind}: {item.ident} done", None)
    logs = ctx.admin.client("logs")
    for item in plan.step(7):
        try:
            logs.delete_log_group(logGroupName=item.ident)
            say(f"  deleted log group {item.ident}", None)
        except logs.exceptions.ResourceNotFoundException:
            pass
    if include_orphans and plan.orphans:
        answer = confirm(f"type 'delete {len(plan.orphans)} orphans' to delete them: ")
        if answer.strip() != f"delete {len(plan.orphans)} orphans":
            say("orphans kept", "yellow")
    say("steps 8-10 (Module 10, bootstrap buckets and keys, partner secrets) are printed above; run them by hand "
        "after exporting state if you want it (OPERATIONS.md, Teardown)", "yellow")
    return 0
