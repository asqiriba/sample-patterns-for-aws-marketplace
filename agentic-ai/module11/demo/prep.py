# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 demo prep --reset | --payments | --warm` (the reset cycle in OPERATIONS.md).
`--baseline` lives in cli_demo.py; its re-seal path is part of `--reset`.

--reset   the reset cycle, steps 1-10, idempotent, each step printed with its result:
          1 stop every in-progress m11-release execution (abandon); wait <= 10 min for an in-progress m11-deploy
            build of a stopped execution; stop in-progress m11-gate builds; cancel a pending promote:production job
          2 abort a non-terminal prod rollout (stopping this rollout's traffic driver first) and wait for ROLLED_BACK
          3 one bot MR `demo/revert-v<n>` that reverts every merge since prod /current (skipped when main equals it)
          4 resync staging to prod /current's sealed manifest
          5-8 latch clear, flags restore, purge (purge-wait rule; a give-up ends the reset with exit 5), reset
          9 close Friday MRs except fallbackMr and stop their environments; 9b close m11-demo-run MRs and delete
            their branches
          10 release prune on prod and staging
--payments  the bot-authored payments MR `demo/payments-v<n>` with PAYMENTS_TWEAK (GitLab only): the bot opens
          it, the approver approves it as a harness-owner, the bot merges it; the merge is the next harness/v<n>
--warm    one invocation per endpoint (dev, staging, prod stable and candidate) and one Logs Insights query per
          runtime log group

Every `demo prep` writes the presenter's `d` shell function into module11/.local/env.sh.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

D_FUNCTION = ('d() { local n="$1"; shift; "$VENV/bin/python" demos/module11_demo.py --section "$n" '
              '--cut="${M11_CUT:-full}" "$@"; }')
D_COMMENT = "# The presenter's one command: d N runs beat N at the M11_CUT level."
FRIDAY_PREFIX = "friday-brevity"
DEMO_RUN_LABEL = "m11-demo-run"
APPROVAL_WAIT_S = 30 * 60
DEPLOY_WAIT_S = 10 * 60
ENV_STOP_WAIT_S = 15 * 60


def _say(text: str, style: str | None = None) -> None:
    from module11.cli import say
    say(text, style)


# ---------------------------------------------------------------- the d function
def ensure_d_function(root: Path, env_path: Path | None = None) -> bool:
    """Write (or replace) the `d` line in module11/.local/env.sh; returns True when the file changed."""
    path = env_path or root / "module11/.local/env.sh"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("d() {") and line != D_COMMENT]
    new = "\n".join(lines + [D_COMMENT, D_FUNCTION]) + "\n"
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


# ---------------------------------------------------------------- pure helpers (tested offline)
def mrs_to_close(open_mrs: Iterable[dict], fallback_iid: int | None) -> tuple[list[dict], list[dict]]:
    """(Friday MRs to close, demo-run MRs to close) for reset step 9 and 9b."""
    friday, demo_run = [], []
    for mr in open_mrs:
        branch = str(mr.get("source_branch") or "")
        if DEMO_RUN_LABEL in (mr.get("labels") or []):
            demo_run.append(mr)
        elif branch.startswith(FRIDAY_PREFIX) and int(mr["iid"]) != (fallback_iid or -1):
            friday.append(mr)
    return friday, demo_run


def revert_files(changed: Iterable[str], read_at_current: Callable[[str], bytes | None]) -> tuple[dict, list[str]]:
    """Files that restore /current's content for every path changed since it: (files to write, paths to delete)."""
    files, deleted = {}, []
    for path in sorted(set(changed)):
        data = read_at_current(path)
        if data is None:
            deleted.append(path)
        else:
            files[path] = data
    return files, deleted


def next_number(tags: Iterable[str]) -> int:
    from module11.aws import gitlab_api as G
    numbers = [n for t in tags if (n := G.tag_number(t)) is not None]
    return (max(numbers) if numbers else 0) + 1


# ---------------------------------------------------------------- live plumbing
def _gitlab(ctx):
    import os
    from module11.aws import gitlab_api as G
    return G.from_secret(ctx.admin.client("secretsmanager"),
                         os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"),
                         os.environ.get("M11_GITLAB_PROJECT_ID", ""))


def _cli(root: Path, *args: str) -> int:
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI with the prep steps' fixed arguments
    return subprocess.run([sys.executable, "-m", "module11", *args], cwd=root).returncode


def _wait_approved_and_merge(gl, iid: int, *, wait_s: int = APPROVAL_WAIT_S, poll: float = 20.0) -> dict:
    """Poll the MR for up to 30 minutes and merge as the bot once its pipeline passed and it has every approval the
    design requires (module11/release/approvals.py), not just GitLab's approved flag."""
    import os
    from module11.aws import bot_changes as B
    from module11.release import approvals as AP
    approvers = AP.parse_approvers(os.environ.get("M11_APPROVER_USERS"))     # fail closed before asking anyone
    _say(f"approve !{iid} as a harness-owner in GitLab (the bot authored it, so any harness-owner may approve)",
         "yellow")
    return B.wait_and_merge(gl, iid, approvers, wait_s=wait_s, poll=poll, say=lambda t: _say(t))


# ---------------------------------------------------------------- --payments
def payments(root: Path, ctx) -> int:
    from module11.aws import bot_changes as B
    from module11.demo import state as S, wording
    gl = _gitlab(ctx)
    n = next_number(t["name"] for t in gl.tags())
    prompt = gl.file(wording.COMPANION_PROMPT, "main")
    if prompt is None:
        raise RuntimeError(f"{wording.COMPANION_PROMPT} is missing on main")
    text = prompt.decode("utf-8")
    if wording.PAYMENTS_TWEAK in text:
        _say("main already declares the payments edit; nothing to open", "yellow")
        return 0
    change = B.open_change(gl, branch=f"demo/payments-v{n}",
                           files={wording.COMPANION_PROMPT: wording.apply_edit(text, wording.PAYMENTS_TWEAK).encode()},
                           message="payments: skip the runbook lookup on payments investigations",
                           title=f"payments: a few words for cheaper payments investigations (harness/v{n})",
                           description=("The payments team's edit (demo): "
                                        f"\"{wording.PAYMENTS_TWEAK}\" Bot-authored, so it gets the API gate only "
                                        "and no UI preview."), labels=())
    _say(f"payments MR !{change.iid} on {change.branch}" + (" (reused)" if change.reused else ""))
    _wait_approved_and_merge(gl, change.iid)
    S.update(lambda doc: {**doc, "payments": {"mr": change.iid, "branch": change.branch, "expectedTag": f"harness/v{n}",
                                              "mergedAt": S.now_iso()}})
    _say(f"the merge becomes harness/v{n}; the release pipeline runs the staging gate and waits at Approval", "green")
    return 0


# ---------------------------------------------------------------- --warm
def warm(root: Path, ctx) -> int:
    """One invocation per endpoint through the evaluation runner's invoke path, and one Logs Insights query
    per runtime log group. Sessions are one-shot, so this exercises each endpoint end to end."""
    import uuid
    from module11.aws import params
    from module11.cli_ops import PROBE_INPUT
    from module11.release import naming
    ssm = ctx.admin.client("ssm")
    logs = ctx.admin.client("logs")
    failed = 0
    for stage in ("dev", "staging", "prod"):
        current = params.stage_get(ssm, stage, "current")
        if not current:
            _say(f"{stage}: no /current; skipped", "yellow")
            continue
        dep = current["deployment"][stage]
        runner = ctx.assume(naming.eval_runner_role(stage), stage, f"demo prep --warm {stage}")
        core = runner.client("bedrock-agentcore", region_name=ctx.region)
        for qualifier in sorted(dep.get("endpoints") or {}):
            session = f"m11-{stage}-warm-{qualifier}-{uuid.uuid4()}"
            envelope = {"identity": {"team": "platform", "principal": "m11-warm-up"},
                        "request": {"change_summary": PROBE_INPUT, "request_id": f"warm-{uuid.uuid4().hex[:12]}"},
                        "context": {"variant": "shadow", "shadow": True, "manifestVersion": current["manifestVersion"],
                                    "behaviorDigest": current["digests"]["behavior"], "release": dep["release"]}}
            started = time.time()
            try:
                response = core.invoke_agent_runtime(agentRuntimeArn=dep["runtimeArn"], qualifier=qualifier,
                                                     runtimeSessionId=session, contentType="application/json",
                                                     accept="text/event-stream", payload=json.dumps(envelope).encode())
                response["response"].read()
                _say(f"{stage}/{qualifier}: warm-up session in {time.time() - started:.1f} s (shadow, no side effects)")
            except Exception as exc:  # noqa: BLE001 - reported per endpoint
                failed += 1
                _say(f"{stage}/{qualifier}: warm-up failed ({type(exc).__name__}: {str(exc)[:200]})", "red")
            finally:
                try:
                    core.stop_runtime_session(agentRuntimeArn=dep["runtimeArn"], qualifier=qualifier,
                                              runtimeSessionId=session)
                except Exception:  # noqa: BLE001
                    pass
            group = f"/aws/bedrock-agentcore/runtimes/{dep['runtimeId']}-{qualifier}"
            try:
                q = logs.start_query(logGroupName=group, startTime=int(time.time()) - 3600, endTime=int(time.time()),
                                     queryString="fields @timestamp | limit 1")
                _say(f"{stage}/{qualifier}: Logs Insights warm-up query {q['queryId']}")
            except Exception as exc:  # noqa: BLE001 - a missing group is reported, not fatal
                _say(f"{stage}/{qualifier}: Logs Insights warm-up skipped ({type(exc).__name__})", "yellow")
    return 1 if failed else 0


# ---------------------------------------------------------------- --reset
def _step(n: str, text: str) -> None:
    _say(f"[{n}] {text}", "bold")


def _stop_executions(ctx, gl) -> None:
    from module11.demo import rearm as R
    cp, cb = ctx.admin.client("codepipeline"), ctx.admin.client("codebuild")
    running = [e for e in cp.list_pipeline_executions(pipelineName=R.PIPELINE, maxResults=20)
               ["pipelineExecutionSummaries"] if e["status"] == "InProgress"]
    for e in running:
        eid = e["pipelineExecutionId"]
        try:
            cp.stop_pipeline_execution(pipelineName=R.PIPELINE, pipelineExecutionId=eid, abandon=True,
                                       reason="demo reset")
            _say(f"  stopped {eid} (abandon)")
        except Exception as exc:  # noqa: BLE001 - already stopping
            _say(f"  {eid}: {type(exc).__name__}", "yellow")
        for row in R.actions(cp, eid):
            build = ((row.get("output") or {}).get("executionResult") or {}).get("externalExecutionId") \
                or (row.get("input") or {}).get("configuration", {}).get("ProjectName")
            if row.get("status") != "InProgress" or not build:
                continue
            if row.get("actionName") == "Gate":
                cb.stop_build(id=build)
                _say(f"  stopped m11-gate build {build}")
            elif row.get("actionName") == "Deploy":
                end = time.time() + DEPLOY_WAIT_S
                while time.time() < end:
                    status = cb.batch_get_builds(ids=[build])["builds"][0]["buildStatus"]
                    if status != "IN_PROGRESS":
                        break
                    time.sleep(15)
                _say(f"  m11-deploy build {build}: {status}")
    for pipe in gl.pipelines(ref="main")[:5]:
        for job in gl.pipeline_jobs(pipe["id"]):
            if job.get("name") == "promote:production" and job.get("status") in ("manual", "created", "pending",
                                                                                   "waiting_for_resource"):
                gl.cancel_job(job["id"])
                _say(f"  cancelled promote:production job {job['id']}")


def reset(root: Path, ctx, *, yes: bool = False) -> int:
    from module11.aws import params
    from module11.demo import commands, rearm as R, state as S
    if not yes:
        answer = input("demo prep --reset stops pipeline executions and resets prod's demo state. Type 'reset': ")
        if answer.strip() != "reset":
            _say("not confirmed; nothing changed", "yellow")
            return 2
    gl = _gitlab(ctx)
    ssm = ctx.admin.client("ssm")
    ports = R.Ports(**commands._ports_common(ctx, "prod"))
    _step("1", "pipeline")
    _stop_executions(ctx, gl)
    _step("2", "rollout")
    item = dict(ports.read_control() or {})
    try:
        if item.get("state") in R.NON_TERMINAL:
            item = dict(R.settle(ports, item, item["rolloutId"], "demo reset"))
        elif item.get("rolloutId"):
            R.stop_driver(ports, item["rolloutId"])
    except R.Exit as exc:
        _say(exc.message, "red")
        return exc.code
    _step("3", "revert every merge since prod /current")
    current = params.stage_get(ssm, "prod", "current")
    cur_sha = (current.get("git") or {}).get("commit")
    main_sha = gl.resolve("main")
    if main_sha.startswith(cur_sha or "~") or cur_sha == main_sha:
        _say("  main equals prod /current; skipped")
    else:
        from module11.aws import bot_changes as B
        diff = gl.get(f"{gl.p}/repository/compare?from={cur_sha}&to={main_sha}") or {}
        changed = {d["new_path"] for d in diff.get("diffs") or []} | {d["old_path"] for d in diff.get("diffs") or []}
        files, deleted = revert_files(changed, lambda p: gl.file(p, cur_sha))
        if deleted:
            _say(f"  paths added since /current are left in place for review: {deleted}", "yellow")
        n = next_number(t["name"] for t in gl.tags())
        change = B.open_change(gl, branch=f"demo/revert-v{n}", files=files,
                               message=f"demo reset: revert every merge since prod /current ({cur_sha[:8]})",
                               title=f"demo reset: revert to prod /current ({cur_sha[:8]})",
                               description="Bot-authored reset (reset step 3); the release computes noop.")
        _wait_approved_and_merge(gl, change.iid)
    _step("4", "staging resync")
    if _cli(root, "release", "deploy", "--stage", "staging", "--manifest", current["manifestVersion"]) != 0:
        _say("  staging resync failed; re-run the reset", "red")
        return 1
    _step("5-8", "latch clear, flags restore, purge, controller reset")
    item = dict(ports.read_control() or {})
    if item.get("rolloutId") and (item.get("rolledBackAt") or item.get("promotedAt") or item.get("state") != "IDLE"):
        try:
            R.cleanup(ports, item, "demo reset")
        except R.Exit as exc:
            _say(exc.message.replace("nothing retried; re-run the command", "reset stopped here; fix and re-run"),
                 "red")
            return 5 if "purge" in exc.message else 1
    _step("9", "Friday and demo-run MRs")
    fallback = (S.read().get("fallbackMr") or {}).get("iid") if isinstance(S.read().get("fallbackMr"), dict) \
        else S.read().get("fallbackMr")
    friday, demo_run = mrs_to_close(gl.merge_requests(state="opened"), int(fallback) if fallback else None)
    for mr in friday + demo_run:
        gl.put(f"{gl.p}/merge_requests/{mr['iid']}", {"state_event": "close"})
        env = gl.environment(f"review/mr-{mr['iid']}")
        if env and env.get("state") == "available":
            gl.stop_environment(env["id"])
        if mr in demo_run:
            try:
                gl.delete_branch(mr["source_branch"])
            except Exception:  # noqa: BLE001 - already gone
                pass
        _say(f"  closed !{mr['iid']} ({mr['source_branch']})")
    end = time.time() + ENV_STOP_WAIT_S
    for mr in friday + demo_run:
        while time.time() < end:
            env = gl.environment(f"review/mr-{mr['iid']}")
            if not env or env.get("state") == "stopped":
                break
            time.sleep(20)
    _step("10", "prune")
    for stage in ("prod", "staging"):
        _cli(root, "release", "prune", "--stage", stage)
    _say("demo reset complete; next: `module11 demo prep --payments`", "green")
    return 0


def cmd(args) -> int:
    from module11.cli_live import _context
    ctx = _context(args.root)
    if args.reset:
        return reset(args.root, ctx, yes=args.yes)
    if args.payments:
        return payments(args.root, ctx)
    if args.warm:
        return warm(args.root, ctx)
    raise ValueError("choose --baseline, --reset, --payments or --warm")
