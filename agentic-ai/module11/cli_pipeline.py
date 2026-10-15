# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pipeline's CodeBuild entry points and the pipeline operator commands (registered by cli.py).

Pipeline actions (each runs in its m11-<project> CodeBuild role):
  pipeline analyze --exports F    Analyze: hash, detect vs prod /current, C1-C16 with c16Base, offline tests,
                                  image-tag check and context upload, the tooling archive; CodePipeline variables
  pipeline seal --exports F       Seal: sealed objects, the bot's annotated tag, Release and generic package
  pipeline deploy --stage S       Staging/Production deploy (or `--action prune`), with the per-step timing record
  pipeline gate                   the change plan's gate on staging (or the rebaseline/evalset procedure)
  pipeline bake                   wait for PROMOTED / ROLLED_BACK, prune prod, post the outcome note
  pipeline seal-experiment        m11-seal-experiment

Operator commands (audited where they change state):
  pipeline build-image [--pin]    build m11/build-image (lever 4) with m11-image and pin its digest
  pipeline timing --execution ID  the Production-stage timing table
  freeze show | set <state>       the account's release freeze (/m11/devops-companion/freeze)

Values the builds receive from CodePipeline are validated before use; secrets never reach a log.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .release import naming

PIPELINE = "m11-release"
BUILD_IMAGE_REPOSITORY = "m11/build-image"
BUILD_IMAGE_FILES = ("module11/pipeline/build-image/Dockerfile", "module11/requirements.txt",
                     "module11/requirements-pulumi.lock")
TOOLING_DIRS = ("module11/release", "module11/aws", "module11/evaluation", "module11/lambdas",
                "module11/infrastructure", "module11/pipeline", "module11/gitlab", "module11/vercel", "module11/edge",
                "module11/corpus", "module11/demo", "module11/runtime", "module11/harness/evaluation",
                "module11/harness/pipeline")
TOOLING_SKIP = {"__pycache__", "venv", ".local", ".pytest_cache", "node_modules"}
EXECUTION_ID = re.compile(r"^[0-9a-f-]{36}$")
MANIFEST_VERSION = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ/[0-9a-f]{7}$")
BAKE_LIMIT_SECONDS = 2100 * 60
BAKE_POLL_SECONDS = 15
TERMINAL = ("PROMOTED", "ROLLED_BACK")


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def _ctx(root: Path):
    from .cli_live import _context
    return _context(root)


def write_exports(path: str | None, values: dict[str, Any]) -> None:
    """`export K='v'` lines for the buildspec to `source` (CodeBuild exported-variables)."""
    lines = [f"export {k}={shlex.quote(str(v))}" for k, v in values.items()]
    for line in lines:
        print(line)
    if path:
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _execution_id() -> str | None:
    value = os.environ.get("PIPELINE_EXECUTION_ID", "")
    return value if EXECUTION_ID.match(value) else None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_mv() -> str:
    mv = os.environ.get("MANIFEST_VERSION", "")
    if not MANIFEST_VERSION.match(mv):
        raise ValueError("MANIFEST_VERSION is missing or invalid")
    return mv


# ---------------------------------------------------------------- tooling archive
def tooling_files(root: Path) -> list[str]:
    """main's tooling: the release engines, AWS adapters, Pulumi programs and pack, Lambdas, pipeline and
    GitLab/Vercel files, the gate definition, the CLI, the requirements, and the Module 10 files the
    edge and runtime closures name."""
    from .release import artifacts as A
    from .release import closure as closures
    out: set[str] = set()
    for rel in TOOLING_DIRS:
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            parts = set(path.relative_to(root).parts)
            if path.is_file() and not path.is_symlink() and not parts & TOOLING_SKIP and path.suffix != ".pyc":
                out.add(path.relative_to(root).as_posix())
    for path in (root / "module11").glob("*.py"):
        out.add(path.relative_to(root).as_posix())
    for path in (root / "module11").glob("requirements*"):
        out.add(path.relative_to(root).as_posix())
    out |= {path for path, _ in closures.edge_entries(root) if path.startswith("module10/")}   # `<path> as <name>` lines
    out |= {p for p in closures.read_list(root, "runtime.txt") or [] if p.startswith("module10/")}
    out.add(A.MODULE10_INCIDENT)       # the reporting-service evidence the sealer reads
    from .gitlab.seed import REUSED_FILES      # Modules 5, 7 and 9 code the tooling imports or reads
    out |= set(REUSED_FILES)
    return sorted(p for p in out if (root / p).is_file())


def tooling_archive(root: Path) -> bytes:
    from .release import closure as closures
    return closures.deterministic_zip([(p, (root / p).read_bytes()) for p in tooling_files(root)])


# ---------------------------------------------------------------- analyze
def _own_kinds(root: Path, declared: dict) -> list[str] | None:
    """The kinds of this commit's own diff against its first parent (for C16's message)."""
    from .cli import declared_at
    from .release import detect as DT
    from .release import manifest as M
    from .release import stage as cfg
    try:
        parent = declared_at(root, "HEAD~1")
    except RuntimeError:
        return None
    rules, unknown, _ = cfg.change_types(root)
    plan = DT.detect(M.hash_set(declared), M.hash_set(parent) if parent else None, rules, unknown,
                     new_tools=declared.get("tools"), base_tools=(parent or {}).get("tools"))
    return list(plan.kinds)


def cmd_analyze(args) -> int:
    from .aws import params, pipeline as AP, releases
    from .cli import git_info
    from .release import artifacts as A
    from .release import check as C
    from .release import detect as DT
    from .release import manifest as M
    from .release import seal as SEAL
    from .release import stage as cfg
    from .release.hashing import hex_of
    started = time.time()
    root = args.root
    ctx = _ctx(root)
    s3, ssm, ecr = ctx.admin.client("s3"), ctx.admin.client("ssm"), ctx.admin.client("ecr")
    git = git_info(root)
    declared = M.build_declared(root, git)
    rules, unknown, _ = cfg.change_types(root)
    prod = params.stage_get(ssm, "prod", "current")
    base = json.loads(releases.get(s3, prod["s3Uri"])) if prod else None

    def read_manifest(commit: str) -> dict:
        return json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/tooling/{commit}.manifest.json"))

    execution = _execution_id()
    runs = AP.executions(ctx.admin.client("codepipeline"))
    c16_manifest, c16_source = DT.c16_base(runs, read_manifest, base, execution)
    if c16_source == "fallback":
        _say("WARN c16_base_fallback: the in-flight lookup failed; C16 is checked against prod /current", "yellow")
    c16_mv = (c16_manifest or {}).get("manifestVersion")
    plan = DT.detect(M.hash_set(declared), M.hash_set(base) if base else None, rules, unknown,
                     base_manifest_version=(prod or {}).get("manifestVersion"), new_tools=declared.get("tools"),
                     base_tools=(base or {}).get("tools"), c16_base=c16_mv)
    _say(f"change plan vs prod /current {plan.baseManifestVersion}: kinds={plan.kinds} releaseKind={plan.releaseKind} "
         f"scope={plan.evalScope} approval={plan.approval} rollout={plan.rollout}")
    for reason in plan.reasons:
        _say(f"  {reason}")
    c16_plan = DT.detect(M.hash_set(declared), M.hash_set(c16_manifest) if c16_manifest else None, rules, unknown,
                         base_manifest_version=c16_mv, new_tools=declared.get("tools"),
                         base_tools=(c16_manifest or {}).get("tools"))
    own = _own_kinds(root, declared)
    commit_range = DT.commit_range(((prod or {}).get("git") or {}).get("commit"), git["commit"])
    findings = C.run(root, c16_plan, has_base=c16_manifest is not None, own_kinds=own, commit_range=commit_range)
    errors = C.errors(findings)
    for f in findings:
        _say(str(f), "red" if f.severity == "error" else "yellow")
    _say(f"checks: {len(errors)} error(s); C16 against {c16_source} {c16_mv}")
    if errors:
        return 1
    if not args.skip_tests:
        tests = sorted(str(p.relative_to(root)) for p in (root / "tests").glob("test_module11_*.py")
                       if not p.name.startswith("test_module11_live"))
        env = {**os.environ, "M11_SIM_RUNS": os.environ.get("M11_SIM_RUNS", "1000")}
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; pytest on the repo's own tests/test_module11_*.py
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests], cwd=root,
                                env=env)
        if result.returncode != 0:
            _say("offline tests failed", "red")
            return 1
    # Image: rebuild only when the plan needs it and the context's tag is not in ECR yet.
    context_hex = hex_of(declared["build"]["imageContext"])
    rebuild = bool(plan.imageRebuild) and not releases.image_digest(ecr, naming.IMAGE_REPOSITORY, context_hex)
    if rebuild:
        written = releases.put_absent(s3, ctx.release_bucket, f"builds/{context_hex}.zip", SEAL.image_context(root),
                                      content_type="application/zip")
        _say(f"image context builds/{context_hex}.zip {'uploaded' if written else 'already present'}")
    # Tooling (every analyzed main commit): tooling/<commit>.zip, its declared manifest, and the pointer.
    data = tooling_archive(root)
    sha = hashlib.sha256(data).hexdigest()
    key = f"tooling/{git['commit']}.zip"
    releases.put_absent(s3, ctx.release_bucket, key, data, metadata={"m11-sha256": sha}, content_type="application/zip")
    s3.put_object(Bucket=ctx.release_bucket, Key=f"tooling/{git['commit']}.manifest.json",
                  Body=json.dumps(declared, sort_keys=True).encode("utf-8"), ContentType="application/json")
    s3.put_object(Bucket=ctx.release_bucket, Key="tooling/main.json", ContentType="application/json",
                  Body=json.dumps({"commit": git["commit"], "key": key, "sha256": sha,
                                   "manifestVersion": declared["manifestVersion"]}).encode("utf-8"))
    if execution:
        releases.put_json(s3, ctx.release_bucket, f"releases/plans/{execution}.json",
                          {"commit": git["commit"], "plan": plan.to_dict(), "c16Base": c16_mv, "c16Source": c16_source})
    variables = plan.variables()
    if plan.releaseKind != "deploy":
        variables["APPROVAL"] = "none"
    variables["IMAGE_REBUILD"] = "true" if rebuild else "false"
    write_exports(args.exports, {**variables, "IMAGE_CONTEXT": context_hex, "COMMIT": git["commit"]})
    _say(f"analyze done in {int(time.time() - started)} s ({len(tooling_files(root))} tooling files)", "green")
    return 0


# ---------------------------------------------------------------- seal
def release_tag(gl, commit: str, approval: str) -> tuple[str, bool]:
    """(tag, reused): reuse a harness/v* tag at this commit whose `approval:` line matches,
    else mint the next number."""
    from .aws import gitlab_api as G
    tags = gl.tags()
    for t in tags:
        if G.tag_number(t["name"]) is not None and (t.get("commit") or {}).get("id") == commit:
            if G.approval_line(t.get("message")) == approval:
                return t["name"], True
            raise RuntimeError(f"{t['name']} points at this commit with a different approval line; "
                               "a new plan level needs a new commit")
    return G.next_tag(t["name"] for t in tags), False


def tag_message(tag: str, plan, mv: str, behavior: str | None = None) -> str:
    lines = [f"{tag}: {', '.join(plan.kinds) or 'no change'}", "", f"approval: {plan.approval}",
             f"manifestVersion: {mv}", f"releaseKind: {plan.releaseKind}", f"rollout: {plan.rollout}",
             f"evalScope: {plan.evalScope}"]
    if behavior:
        lines.append(f"behaviorDigest: {behavior}")
    return "\n".join(lines)


def cmd_seal(args) -> int:
    from .aws import gitlab_api as G
    from .aws import ledger, params, releases
    from .cli import git_info, tree_at
    from .release import detect as DT
    from .release import manifest as M
    from .release import seal as SEAL
    from .release.hashing import hex_of
    started = time.time()
    root = args.root
    ctx = _ctx(root)
    s3, ssm = ctx.admin.client("s3"), ctx.admin.client("ssm")
    execution = _execution_id()
    git = git_info(root)
    if execution:
        stored = json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/releases/plans/{execution}.json"))
        if stored["commit"] != git["commit"]:
            raise RuntimeError("the Analyze plan is for another commit")
        plan = DT.ChangePlan(**stored["plan"])
    else:
        raise RuntimeError("pipeline seal runs only inside m11-release (PIPELINE_EXECUTION_ID); "
                           "operators use `release seal --operator`")
    gl = G.from_secret(ctx.admin.client("secretsmanager"), os.environ.get("M11_GITLAB_TOKEN_SECRET",
                                                                          "devops-companion/partners/gitlab-token"),
                       os.environ["M11_GITLAB_PROJECT_ID"])
    tag, reused = release_tag(gl, git["commit"], plan.approval)
    with tree_at(root, git["commit"]) as tree:
        declared = M.build_declared(tree, {**git, "tag": tag})
        mv = declared["manifestVersion"]
        context_hex = hex_of(declared["build"]["imageContext"])
        digest = releases.image_digest(ctx.admin.client("ecr"), naming.IMAGE_REPOSITORY, context_hex)
        if not digest:
            raise RuntimeError(f"image {naming.IMAGE_REPOSITORY}:{context_hex[:16]}… does not exist (Image stage skipped "
                               "for a context that was never built)")
        layer = SEAL.build_layer(tree / "module11/requirements-lambda.txt")
        ops_layer = SEAL.build_layer(tree / SEAL.OPS_REQUIREMENTS) if SEAL.has_operations(tree) else None
        objects, record, final = SEAL.seal_objects(tree, declared, image_digest=digest, layer_zip=layer,
                                                   change_plan=plan.to_dict(),
                                                   sealed_by=ctx.admin.client("sts").get_caller_identity()["Arn"],
                                                   sealed_at=_now(), ops_layer_zip=ops_layer)
    counts = releases.upload(s3, ctx.release_bucket, objects)
    record_bytes = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    releases.put_absent(s3, ctx.release_bucket, f"releases/{mv}/seal.json", record_bytes, content_type="application/json")
    behavior = final["digests"]["behavior"]
    if not reused:
        gl.create_tag(tag, git["commit"], tag_message(tag, plan, mv, behavior))
        _say(f"tag {tag} created by the bot (approval: {plan.approval})")
    else:
        _say(f"tag {tag} already points at {git['commit'][:12]}; reused with its Release and package")
    number = G.tag_number(tag)
    present = set(gl.package_files(number))
    manifest_bytes = json.dumps(final, indent=2, sort_keys=True).encode("utf-8")
    plan_bytes = json.dumps(plan.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    for name, body in (("manifest.json", manifest_bytes), ("change-plan.json", plan_bytes)):
        if name not in present:
            gl.upload_package_file(number, name, body)
    if gl.release(tag) is None:
        links = [{"name": n, "url": gl.package_url(number, n), "link_type": "package"}
                 for n in ("manifest.json", "change-plan.json")]
        gl.create_release(tag, tag, f"Release {tag} ({mv}): {', '.join(plan.kinds)}\n\n"
                                    f"- approval: {plan.approval}\n- rollout: {plan.rollout}\n- behaviorDigest: {behavior}",
                          links)
    ledger.put(s3, ctx.release_bucket, "shared", "pipeline-seal", by=record["sealedBy"], manifestVersion=mv,
               commit=git["commit"], tag=tag, reused=reused, execution=execution, objects=counts)
    write_exports(args.exports, {"RELEASE_TAG": tag, "MANIFEST_VERSION": mv, "BEHAVIOR_DIGEST": behavior})
    _say(f"sealed {mv} as {tag}: {counts['written']} objects written, {counts['present']} present "
         f"({int(time.time() - started)} s)", "green")
    return 0


# ---------------------------------------------------------------- deploy / prune
def cmd_deploy(args) -> int:
    from .aws import deploy as AD
    from .aws import releases
    if args.stage not in ("staging", "prod"):
        raise ValueError("the pipeline deploys staging and prod only")
    kind = os.environ.get("RELEASE_KIND", "deploy")
    if kind in ("rebaseline", "evalset"):
        _say(f"releaseKind {kind}: nothing to deploy; the Gate action runs that procedure against prod")
        return 0
    started = time.time()
    ctx = _ctx(args.root)
    deploy = AD.Deploy(ctx, args.stage)
    if args.action == "prune":
        result = deploy.prune()
        _say(f"pruned {args.stage}: granted {result['granted']['toolArns']}", "green")
        return 0
    mv = _env_mv()
    result = deploy.run(mv)
    timings = {"build": {k: os.environ.get(k) for k in ("CODEBUILD_BUILD_ID", "CODEBUILD_START_TIME")},
               "steps": result.get("timings", {}), "path": result["path"], "seconds": round(time.time() - started, 1),
               "execution": _execution_id(), "release": os.environ.get("RELEASE_TAG")}
    releases.put_json(deploy.s3, deploy.rb, f"release-inputs/{args.stage}/{mv}.timing.json", timings)
    _say(json.dumps({"m11.deploy.timing": timings}))
    _say(f"deployed {mv} to {args.stage}: path {result['path']} in {result['seconds']} s", "green")
    return 0


# ---------------------------------------------------------------- gate
def _notes(ctx, operation: dict) -> dict:
    lam = ctx.admin.client("lambda")
    response = lam.invoke(FunctionName=os.environ.get("M11_NOTES_FUNCTION", "m11-gitlab-notes"),
                          Payload=json.dumps(operation).encode("utf-8"))
    body = json.loads(response["Payload"].read() or b"{}")
    if not body.get("ok"):
        _say(f"WARN notes broker {operation.get('operation')}: {body.get('error')} {body.get('message', '')}", "yellow")
    return body


def cmd_gate(args) -> int:
    from .cli_eval import EXIT_OVER_BUDGET, cmd_gate as run_gate
    from .release import detect as DT
    from .aws import releases
    kind = os.environ.get("RELEASE_KIND", "deploy")
    ctx = _ctx(args.root)
    if kind in ("rebaseline", "evalset"):
        from .cli_eval import reusable_record
        reused = reusable_record(ctx, args.root, kind)
        if reused:
            _say(reused, "green")
            return 0
    if kind == "rebaseline":
        from .cli_eval import cmd_calibrate
        return cmd_calibrate(_Args(root=args.root, manifest="prod-current", n=8, r=3, resume=None, max_usd=250.0))
    if kind == "evalset":
        from .cli_eval import cmd_evalset_rebaseline
        return cmd_evalset_rebaseline(_Args(root=args.root, old_ref="HEAD~1", from_version=None, to_version=None))
    scope = os.environ.get("EVAL_SCOPE", "full")
    if not re.match(r"^(full|smoke|none|tag:[a-z0-9_]+)$", scope):
        raise ValueError("EVAL_SCOPE is invalid")
    if scope in ("smoke", "none"):
        _say(f"eval scope {scope}: the deploy's smoke test and edge probes are this release's gate")
        return 0
    execution = _execution_id()
    repeats = None
    if execution:
        stored = json.loads(releases.get(ctx.admin.client("s3"), f"s3://{ctx.release_bucket}/releases/plans/{execution}.json"))
        repeats = DT.ChangePlan(**stored["plan"]).repeatsOverride or None
    gate_args = _Args(root=args.root, stage="staging", profile="staging", scope=scope, scenario=None,
                      from_run=None, no_write=False, max_usd=args.max_usd, repeats=repeats, warmup=args.warmup)
    code = run_gate(gate_args)
    verdict = {0: "PASS", 2: "FAIL", 3: "INCONCLUSIVE", EXIT_OVER_BUDGET: "OVER BUDGET"}.get(code, f"exit {code}")
    commit = os.environ.get("COMMIT", "")
    if re.match(r"^[0-9a-f]{40}$", commit):
        text = (f"**m11 staging gate: {verdict}** for {os.environ.get('RELEASE_TAG', '')} "
                f"({os.environ.get('MANIFEST_VERSION', '')}, scope {scope}). Report under "
                f"`s3://{ctx.release_bucket}/evaluations/staging/`.")
        if code in (2, 3):
            text += "\n\n`main` is ahead of prod; revert or fix forward before landing flag or edge changes."
        report = getattr(gate_args, "report", None)
        if report:
            text += "\n\n" + report["markdown"]
        _notes(ctx, {"operation": "commit_note", "sha": commit, "body": text[:60000]})
    return code


class _Args:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


# ---------------------------------------------------------------- bake
def bake_outcome(item: dict | None, manifest_version: str) -> str | None:
    """None while this release's rollout runs; 'none' when no rollout was opened for it; else the terminal state."""
    if not item or item.get("manifestVersion") != manifest_version:
        return "none"
    state = item.get("state", "IDLE")
    if state in TERMINAL:
        return state
    if state == "IDLE":
        return "none"
    return None


def cmd_bake(args) -> int:
    from .aws import control_table
    from .aws import deploy as AD
    mv = _env_mv()
    ctx = _ctx(args.root)
    ddb = ctx.admin.client("dynamodb")
    table = naming.table("prod", "control")
    deadline = time.time() + (args.limit_seconds or BAKE_LIMIT_SECONDS)
    outcome, last = None, None
    while outcome is None:
        item = control_table.get(ddb, table)
        outcome = bake_outcome(item, mv)
        state = (item or {}).get("state")
        if state != last:
            _say(f"{_now()} prod rollout {(item or {}).get('rolloutId')} {state}")
            last = state
        if outcome is None:
            if time.time() > deadline:
                outcome = "BAKE_TIMEOUT"
                break
            time.sleep(BAKE_POLL_SECONDS)
    if outcome in TERMINAL:
        result = AD.Deploy(ctx, "prod").prune()
        _say(f"pruned prod after {outcome}: granted tool versions {result['granted']['toolArns']}")
    commit = os.environ.get("COMMIT", "")
    reason = (item or {}).get("reason") or ""
    text = {"PROMOTED": "PROMOTED", "ROLLED_BACK": f"ROLLED_BACK ({reason or 'see the controller audit events'})",
            "BAKE_TIMEOUT": "BAKE_TIMEOUT (the rollout continues under the controller)",
            "none": "no rollout: the release deployed without a canary"}[outcome]
    if re.match(r"^[0-9a-f]{40}$", commit):
        body = f"**m11 prod outcome for {os.environ.get('RELEASE_TAG', '')}: {text}** ({mv})"
        if outcome == "ROLLED_BACK":
            body += "\n\n`main` is ahead of prod; revert or fix forward before landing flag or edge changes."
        _notes(ctx, {"operation": "commit_note", "sha": commit, "body": body})
    _say(f"bake: {text}", "green" if outcome in ("PROMOTED", "none") else "red")
    return 0 if outcome in ("PROMOTED", "none") else 1


# ---------------------------------------------------------------- build image (lever 4)
def build_image_context(root: Path) -> bytes:
    from .release import closure as closures
    return closures.deterministic_zip([(p, (root / p).read_bytes()) for p in BUILD_IMAGE_FILES])


def cmd_build_image(args) -> int:
    from .aws import releases
    from .cli_live import IMAGE_BUILDSPEC
    ctx = _ctx(args.root)
    data = build_image_context(args.root)
    context_hex = hashlib.sha256(data).hexdigest()
    s3, ecr = ctx.admin.client("s3"), ctx.admin.client("ecr")
    releases.put_absent(s3, ctx.release_bucket, f"builds/{context_hex}.zip", data, content_type="application/zip")
    repo = ecr.describe_repositories(repositoryNames=[BUILD_IMAGE_REPOSITORY])["repositories"][0]
    tag = context_hex[:32]
    digest = releases.build_image(ctx.admin.client("codebuild"), ecr, project="m11-image",
                                  repository=BUILD_IMAGE_REPOSITORY, context_hex=context_hex, tag=tag, log=_say,
                                  buildspec=(args.root / IMAGE_BUILDSPEC).read_text(encoding="utf-8"),
                                  extra_env={"DOCKERFILE": BUILD_IMAGE_FILES[0], "REPOSITORY_URI": repo["repositoryUri"],
                                             "REPOSITORY_NAME": BUILD_IMAGE_REPOSITORY})
    _say(f"m11/build-image:{tag} = {digest}", "green")
    if args.pin:
        platform = args.root / "module11/infrastructure/platform"
        subprocess.run(["pulumi", "-C", str(platform), "config", "set", "--stack", "shared", "buildImage", digest],
                       check=True)
        _say(f"pinned m11-platform:buildImage = {digest}")
    return 0


# ---------------------------------------------------------------- timing
LIMIT_SECONDS = 9 * 60
WARN_SECONDS = 8 * 60


def production_timing(actions: list[dict], phases: list[dict] | None = None, steps: dict | None = None) -> dict:
    """From ListActionExecutions: PutApprovalResult (the approval action's end) to the Production Deploy
    action's end, with the CodeBuild phases and the deploy's own step record."""
    def find(stage: str, action: str) -> dict | None:
        rows = [a for a in actions if a.get("stageName") == stage and a.get("actionName") == action]
        return max(rows, key=lambda a: str(a.get("startTime"))) if rows else None

    approval = find("ExtendedApproval", "ExtendedReleaseApproval") or find("Approval", "ReleaseApproval")
    deploy = find("Production", "Deploy")
    if not approval or not deploy or deploy.get("status") != "Succeeded":
        return {"complete": False}
    approved = approval["lastUpdateTime"]
    done = deploy["lastUpdateTime"]
    total = (done - approved).total_seconds()
    row = {"complete": True, "approvedAt": approved.isoformat(), "deploySucceededAt": done.isoformat(),
           "seconds": round(total, 1), "verdict": "FAIL" if total > LIMIT_SECONDS else
           ("WARN" if total > WARN_SECONDS else "OK"),
           "transition": round((deploy["startTime"] - approved).total_seconds(), 1)}
    if phases:
        row["codebuild"] = {p["phaseType"]: p.get("durationInSeconds") for p in phases if "phaseType" in p}
    if steps:
        row["steps"] = steps
    if row["verdict"] != "OK" and steps:
        row["slowest"] = max(steps, key=lambda k: steps[k])
    return row


def cmd_timing(args) -> int:
    from .aws import releases
    ctx = _ctx(args.root)
    cp = ctx.admin.client("codepipeline")
    actions = cp.list_action_executions(pipelineName=PIPELINE, filter={"pipelineExecutionId": args.execution},
                                        maxResults=100)["actionExecutionDetails"]
    deploy = next((a for a in actions if a.get("stageName") == "Production" and a.get("actionName") == "Deploy"), None)
    phases, steps = None, None
    if deploy:
        build_id = (deploy.get("output", {}).get("executionResult") or {}).get("externalExecutionId")
        if build_id:
            build = ctx.admin.client("codebuild").batch_get_builds(ids=[build_id])["builds"][0]
            phases = build.get("phases")
            env = {v["name"]: v["value"] for v in build.get("environment", {}).get("environmentVariables", [])}
            mv = next((o.get("value") for o in build.get("environment", {}).get("environmentVariables", [])
                       if o.get("name") == "MANIFEST_VERSION"), None)
            mv = mv or env.get("MANIFEST_VERSION")
            if mv and MANIFEST_VERSION.match(mv):
                try:
                    doc = json.loads(releases.get(ctx.admin.client("s3"),
                                                  f"s3://{ctx.release_bucket}/release-inputs/prod/{mv}.timing.json"))
                    steps = doc.get("steps")
                except Exception:  # noqa: BLE001 - an older deploy without a step record
                    steps = None
    row = production_timing(actions, phases, steps)
    print(json.dumps(row, indent=2, default=str))
    return 0 if row.get("complete") and row["verdict"] != "FAIL" else 1


# ---------------------------------------------------------------- freeze
def cmd_freeze_show(args) -> int:
    from .aws import params
    ctx = _ctx(args.root)
    print(json.dumps(params.freeze(ctx.admin.client("ssm"))))
    return 0


def cmd_freeze_set(args) -> int:
    from .aws import ledger, params
    from .aws import releases
    if args.state not in ("none", "planned", "emergency"):
        raise ValueError("state must be none, planned or emergency")
    ctx = _ctx(args.root)
    ssm = ctx.admin.client("ssm")
    caller = ctx.admin.client("sts").get_caller_identity()["Arn"]
    doc = {"state": args.state, "reason": args.reason or "", "setBy": caller, "setAt": _now()}
    params.put_json(ssm, naming.freeze_param(), doc)
    ledger.put(ctx.admin.client("s3"), releases.bucket_name(ctx.account, ctx.region), "shared", "freeze", by=caller,
               state=args.state, reason=args.reason or "")
    _say(f"freeze {args.state}" + (f" ({args.reason})" if args.reason else ""), "green")
    return 0


def cmd_provider_health(args) -> int:
    """Operator scheduling before a full-scope gate (evaluation/provider_health.py): exit 0 when the model's prior
    30 minutes are healthy (p99 ≤ 7 s, 0 provider 5xx), 1 otherwise. `--probe N` first sends N small model calls
    (a quiet window has too few invocations for a p99) and waits for the metrics. `--record` writes the reading to
    the release ledger, with `--execution` when it was taken for a given pipeline execution."""
    from .aws import ledger, releases
    from .cli_eval import model_ids_of
    from .cli_live import _context
    from .evaluation import provider_health as PH
    ctx = _context(args.root)
    models = model_ids_of(args.root)
    probe = None
    if args.probe:
        probe = PH.probe(ctx.admin.client("bedrock-runtime", region_name=ctx.region), models[0], args.probe)
        _say(f"probe: {probe['ok']}/{probe['calls']} ok at concurrency {probe['concurrency']}, client p50 "
             f"{probe['p50Ms']} ms, p99 {probe['p99Ms']} ms, errors {probe['errors'] or 'none'}; waiting "
             f"{args.metrics_lag} s for the metrics")
        time.sleep(args.metrics_lag)
    doc = PH.reading(ctx.admin.client("cloudwatch", region_name=ctx.region), models, time.time(), args.minutes)
    doc["probe"] = probe
    _say(PH.reading_markdown(doc), "green" if doc["healthy"] else "yellow")
    if args.record:
        caller = ctx.admin.client("sts").get_caller_identity()["Arn"]
        key, _ = ledger.put(ctx.admin.client("s3"), releases.bucket_name(ctx.account, ctx.region), "shared",
                            "provider-health-reading", by=caller, execution=args.execution or "", reading=doc,
                            purpose=args.purpose or "")
        _say(f"ledger: {key}")
    if args.json:
        print(json.dumps(doc, sort_keys=True, default=str))
    return 0 if doc["healthy"] else 1


# ---------------------------------------------------------------- registration
def register(sub) -> None:
    pl = sub.add_parser("pipeline", help="m11-release CodeBuild entry points and pipeline operator commands")
    psub = pl.add_subparsers(dest="pipeline_command", required=True)
    a = psub.add_parser("analyze")
    a.add_argument("--exports")
    a.add_argument("--skip-tests", action="store_true")
    a.set_defaults(fn=cmd_analyze)
    s = psub.add_parser("seal")
    s.add_argument("--exports")
    s.set_defaults(fn=cmd_seal)
    d = psub.add_parser("deploy")
    d.add_argument("--stage", required=True)
    d.add_argument("--action", choices=["deploy", "prune"], default="deploy")
    d.set_defaults(fn=cmd_deploy)
    g = psub.add_parser("gate")
    g.add_argument("--max-usd", type=float, default=60.0)
    g.add_argument("--warmup", type=int, default=None, help="warm-up sessions (default M11_GATE_WARMUP or 12)")
    g.set_defaults(fn=cmd_gate)
    b = psub.add_parser("bake")
    b.add_argument("--limit-seconds", type=int, default=None)
    b.set_defaults(fn=cmd_bake)
    bi = psub.add_parser("build-image", help="build m11/build-image (lever 4) and optionally pin its digest")
    bi.add_argument("--pin", action="store_true")
    bi.set_defaults(fn=cmd_build_image)
    t = psub.add_parser("timing", help="Production-stage timing of one execution (R-b: <= 9:00)")
    t.add_argument("--execution", required=True)
    t.set_defaults(fn=cmd_timing)
    ph = psub.add_parser("provider-health", help="the model's prior-30-minute health before a full-scope gate "
                                                 "(exit 0 healthy: p99 <= 7 s, 0 provider 5xx)")
    ph.add_argument("--minutes", type=int, default=30)
    ph.add_argument("--probe", type=int, default=0, help="send N small model calls first (quiet windows)")
    ph.add_argument("--metrics-lag", type=int, default=150, help="seconds to wait after a probe (default 150)")
    ph.add_argument("--record", action="store_true", help="write the reading to the release ledger")
    ph.add_argument("--execution", help="the pipeline execution the reading was taken for")
    ph.add_argument("--purpose", help="why the reading was taken (ledger)")
    ph.add_argument("--json", action="store_true")
    ph.set_defaults(fn=cmd_provider_health)
    from . import cli_experiment, cli_preview
    cli_experiment.register_pipeline(psub)
    cli_preview.register_pipeline(psub)
    fr = sub.add_parser("freeze", help="the release freeze (/m11/devops-companion/freeze)")
    fsub = fr.add_subparsers(dest="freeze_command", required=True)
    fsub.add_parser("show").set_defaults(fn=cmd_freeze_show)
    fs = fsub.add_parser("set")
    fs.add_argument("state", choices=["none", "planned", "emergency"])
    fs.add_argument("--reason", default="")
    fs.set_defaults(fn=cmd_freeze_set)
