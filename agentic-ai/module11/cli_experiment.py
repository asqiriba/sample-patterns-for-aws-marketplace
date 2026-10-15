# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The shadow experiment: measure an unmerged branch on a stage's real traffic with no user
exposure. It is the only path that runs unmerged inputs on prod, so every step is fixed and audited.

  shadow experiment --source <branch|sha> --stage dev|prod [--hours H] [--wait]
      1. refuse if a freeze or a rollout is active; EXPERIMENT_ID = x-<16 hex>; freeze `planned`,
         reason `experiment <id>` (real releases hold at Approval);
      2. resolve --source to a 40-hex SHA through the GitLab API; assume m11-operator-experiment and start
         m11-seal-experiment with SOURCE_SHA and EXPERIMENT_ID only;
      3. `release deploy --experiment` as the stage deployer: candidate only, open{profile: model,
         experiment: true, shadowPct: 100};
      4. with --wait: send `shadow-evaluate` once `now - openedAt >= hours x 3600`, until the verdict.
  shadow experiment --finish <id>
      5. abort{latch: false} -> ROLLED_BACK, reset, `release deploy --reapply` (the prune), clear the freeze.
  pipeline seal-experiment       m11-seal-experiment's build (main's tooling, the branch archive, scope check)

Traffic is a separate driver (`module11 traffic --stage <s> --until-terminal`), as in a shadow experiment's traffic step.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .release import naming

EXPERIMENT_ID = re.compile(r"^x-[0-9a-f]{16}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
EXPERIMENT_KINDS = frozenset({"model", "instructions", "tool_description", "orchestration"})
EXPERIMENT_STAGES = ("dev", "prod")
PREFIX = "releases/experiments"
SEAL_PROJECT = "m11-seal-experiment"
OPERATOR_ROLE = "m11-operator-experiment"
MAX_HOURS = 30.0                      # the model profile's shadowPhase.maxHours
VERDICT_GRACE_HOURS = 6.0
ACTIVE = ("WARMING", "OBSERVING", "SHADOWING", "ADVANCING", "ROLLING_BACK")


class ExperimentRefused(RuntimeError):
    pass


class ExperimentScopeRejected(RuntimeError):
    pass


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def new_id() -> str:
    return "x-" + secrets.token_hex(8)


def freeze_reason(experiment_id: str) -> str:
    return f"experiment {experiment_id}"


def check_inputs(sha: str, experiment_id: str) -> None:
    """The build's own validation (values are never echoed)."""
    if not isinstance(sha, str) or not SHA.match(sha) or not isinstance(experiment_id, str) \
            or not EXPERIMENT_ID.match(experiment_id):
        raise ExperimentRefused("invalid input")


def check_freeze(freeze: dict, experiment_id: str) -> str:
    """The target stage, if the freeze was set for this experiment; else ExperimentRefused."""
    if freeze.get("state") != "planned" or freeze.get("reason") != freeze_reason(experiment_id):
        raise ExperimentRefused("no planned freeze for this experiment")
    stage = freeze.get("stage")
    if stage not in EXPERIMENT_STAGES:
        raise ExperimentRefused("the freeze names no experiment stage")
    return stage


def check_scope(kinds) -> None:
    extra = sorted(set(kinds) - EXPERIMENT_KINDS)
    if extra or not kinds:
        raise ExperimentScopeRejected(", ".join(extra) if extra else "no bundle or model change")


def experiment_declared(declared: dict) -> dict:
    """The declared manifest with its bundle component under releases/experiments/<mv>/."""
    out = json.loads(json.dumps(declared))
    mv = out["manifestVersion"]
    out["build"]["bundleComponent"]["s3Key"] = f"{PREFIX}/{mv}/bundle-component.json"
    return out


def utc_z(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_archive(data: bytes, out: Path) -> Path:
    """A GitLab archive.zip has one top-level `<project>-<sha>/` directory; return the tree inside it."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        top = {n.split("/", 1)[0] for n in names if "/" in n}
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts:
                raise ExperimentRefused("unsafe archive path")
        archive.extractall(out)
    return out / top.pop() if len(top) == 1 else out


def should_evaluate(opened_at: float, now: float, hours: float, sent: bool) -> bool:
    return not sent and now - float(opened_at) >= hours * 3600


def finish_steps(state: str) -> list[str]:
    """What `--finish` still has to do from a control-item state (re-runnable)."""
    steps = []
    if state in ACTIVE:
        steps += ["abort", "wait-rolled-back"]
    if state in ACTIVE or state in ("ROLLED_BACK", "PROMOTED"):
        steps.append("reset")
    return steps + ["reapply", "unfreeze"]


# ---------------------------------------------------------------- m11-seal-experiment (CodeBuild)
def cmd_seal_experiment(args) -> int:
    from .aws import ledger, params, releases
    from .cli_live import _context
    from .cli_pipeline import _notes
    from .release import detect as DT
    from .release import manifest as M
    from .release import seal as SEAL
    from .release import stage as cfg
    sha, xid = os.environ.get("SOURCE_SHA", ""), os.environ.get("EXPERIMENT_ID", "")
    check_inputs(sha, xid)
    ctx = _context(args.root)
    s3, ssm = ctx.admin.client("s3"), ctx.admin.client("ssm")
    stage = check_freeze(params.freeze(ssm), xid)
    archive = _notes(ctx, {"operation": "archive", "sha": sha})
    commit = _notes(ctx, {"operation": "commit", "ref": sha})
    if not archive.get("ok") or not commit.get("ok"):
        raise ExperimentRefused("the branch archive could not be exported")
    data = releases.get_verified(s3, f"s3://{ctx.release_bucket}/{archive['result']['key']}",
                                 "sha256:" + archive["result"]["sha256"], "branch archive")
    current = params.stage_get(ssm, stage, "current")
    if not current:
        raise ExperimentRefused(f"{stage} has no /current")
    base = json.loads(releases.get(s3, current["s3Uri"]))
    rules, unknown, _ = cfg.change_types(args.root)               # main's rules (the tooling archive)
    with tempfile.TemporaryDirectory(prefix="m11-exp-") as tmp:
        tree = extract_archive(data, Path(tmp))
        git = {"commit": sha, "timestamp": utc_z(commit["result"]["createdAt"]), "tag": None,
               "subject": (commit["result"].get("title") or "")[:300]}
        declared = experiment_declared(M.build_declared(tree, git))
        plan = DT.detect(M.hash_set(declared), M.hash_set(base), rules, unknown,
                         base_manifest_version=current["manifestVersion"], new_tools=declared.get("tools"),
                         base_tools=base.get("tools"))
        check_scope(plan.kinds)
        if declared["build"]["imageContext"] != base["build"]["imageContext"]:
            raise ExperimentScopeRejected("runtime/code")
        layer = SEAL.build_layer(tree / "module11/requirements-lambda.txt")
        ops_layer = SEAL.build_layer(tree / SEAL.OPS_REQUIREMENTS) if SEAL.has_operations(tree) else None
        objects, record, final = SEAL.seal_objects(
            tree, declared, image_digest=base["build"]["imageDigest"], layer_zip=layer, change_plan=plan.to_dict(),
            sealed_by=ctx.admin.client("sts").get_caller_identity()["Arn"],
            sealed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), ops_layer_zip=ops_layer, prefix=PREFIX)
    mv = final["manifestVersion"]
    counts = releases.upload(s3, ctx.release_bucket, objects)
    record["experiment"] = {"id": xid, "stage": stage, "sourceSha": sha}
    releases.put_absent(s3, ctx.release_bucket, f"{PREFIX}/{mv}/seal.json",
                        json.dumps(record, indent=2, sort_keys=True).encode("utf-8"), content_type="application/json")
    releases.put_json(s3, ctx.release_bucket, f"{PREFIX}/by-id/{xid}.json",
                      {"manifestVersion": mv, "stage": stage, "sourceSha": sha, "kinds": plan.kinds})
    ledger.put(s3, ctx.release_bucket, "shared", "experiment", by=record["sealedBy"], experiment=xid, targetStage=stage,
               manifestVersion=mv, sourceSha=sha, kinds=plan.kinds, objects=counts)
    _say(f"experiment {xid} sealed as {mv} for {stage}: kinds {plan.kinds}; {counts['written']} written, "
         f"{counts['present']} present", "green")
    return 0


# ---------------------------------------------------------------- the operator command
class Experiment:
    def __init__(self, root: Path) -> None:
        from .cli_live import _context
        self.root = root
        self.ctx = _context(root)
        self.ssm = self.ctx.admin.client("ssm")
        self.s3 = self.ctx.admin.client("s3")
        self.local = root / "module11/.local/experiments"
        self.local.mkdir(parents=True, exist_ok=True)

    def record(self, xid: str) -> dict:
        path = self.local / f"{xid}.json"
        return json.loads(path.read_text()) if path.is_file() else {"id": xid}

    def save(self, doc: dict) -> None:
        (self.local / f"{doc['id']}.json").write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")

    def control(self, stage: str) -> dict:
        from .aws import control_table
        return control_table.get(self.ctx.admin.client("dynamodb"), naming.table(stage, "control")) or {}

    def invoke(self, deploy, command: str, args: dict) -> dict:
        from .aws import rollouts
        return rollouts.invoke(rollouts.lambda_client(deploy.deployer, self.ctx.region), deploy.stage, command, args,
                               check=False, log=lambda line: _say(line, "yellow"))

    def set_freeze(self, state: str, reason: str, stage: str | None) -> None:
        from .aws import ledger, params, releases
        caller = self.ctx.admin.client("sts").get_caller_identity()["Arn"]
        doc = {"state": state, "reason": reason, "setBy": caller, "setAt": datetime.now(timezone.utc).isoformat()}
        if stage:
            doc["stage"] = stage
        params.put_json(self.ssm, naming.freeze_param(), doc)
        ledger.put(self.s3, releases.bucket_name(self.ctx.account, self.ctx.region), "shared", "freeze", by=caller,
                   state=state, reason=reason, targetStage=stage)

    def seal(self, sha: str, xid: str) -> str:
        from .aws import operator, releases
        session = operator.assume(self.ctx.role_arn(OPERATOR_ROLE), sts=self.ctx.admin.client("sts"),
                                  purpose=f"shadow experiment {xid}", stage="shared",
                                  record=operator.local_record(self.root / "module11/.local/audit"))
        cb = session.client("codebuild", region_name=self.ctx.region)
        build = cb.start_build(projectName=SEAL_PROJECT, environmentVariablesOverride=[
            {"name": "SOURCE_SHA", "value": sha, "type": "PLAINTEXT"},
            {"name": "EXPERIMENT_ID", "value": xid, "type": "PLAINTEXT"}])["build"]
        _say(f"{SEAL_PROJECT} build {build['id']} started as {session.session_name}")
        while True:
            time.sleep(10)
            status = cb.batch_get_builds(ids=[build["id"]])["builds"][0]["buildStatus"]
            if status != "IN_PROGRESS":
                break
        if status != "SUCCEEDED":
            raise ExperimentRefused(f"{SEAL_PROJECT} ended {status}")
        pointer = json.loads(releases.get(self.s3, f"s3://{self.ctx.release_bucket}/{PREFIX}/by-id/{xid}.json"))
        return pointer["manifestVersion"]

    def start(self, source: str, stage: str, hours: float) -> dict:
        from .aws import gitlab_api as G
        from .aws import deploy as AD
        from .aws import params
        if stage not in EXPERIMENT_STAGES:
            raise ValueError("--stage must be dev or prod")
        if not 0 < hours <= MAX_HOURS:
            raise ValueError(f"--hours must be in (0, {MAX_HOURS}] (shadowPhase.maxHours)")
        freeze = params.freeze(self.ssm)
        if freeze.get("state") != "none":
            raise ExperimentRefused(f"a freeze is active ({freeze.get('state')}: {freeze.get('reason')})")
        state = self.control(stage).get("state", "IDLE")
        if state in ACTIVE:
            raise ExperimentRefused(f"{stage} has a rollout in progress ({state})")
        gl = G.from_secret(self.ctx.admin.client("secretsmanager"),
                           os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"),
                           os.environ.get("M11_GITLAB_PROJECT_ID", ""))
        sha = source if SHA.match(source) else gl.resolve(source)
        xid = new_id()
        doc = {"id": xid, "stage": stage, "source": source, "sourceSha": sha, "hours": hours,
               "startedAt": time.time()}
        self.save(doc)
        self.set_freeze("planned", freeze_reason(xid), stage)
        _say(f"experiment {xid}: freeze planned ({freeze_reason(xid)}); source {source} = {sha[:12]}")
        try:
            doc["manifestVersion"] = self.seal(sha, xid)
            self.save(doc)
            deploy = AD.Deploy(self.ctx, stage)
            result = deploy.run(doc["manifestVersion"], experiment=xid)
        except Exception:
            self.save({**doc, "failedAt": time.time()})
            raise
        doc.update(rolloutId=result["rolloutId"], openedAt=time.time(), candidateVersion=result["runtimeVersion"])
        self.save(doc)
        _say(f"experiment {xid} open on {stage}: rollout {result['rolloutId']} ({result.get('state')}), candidate "
             f"v{result['runtimeVersion']} shadowed at 100%", "green")
        return doc

    def wait(self, xid: str, poll: float = 30.0) -> dict:
        from .aws import deploy as AD
        doc = self.record(xid)
        deploy = AD.Deploy(self.ctx, doc["stage"])
        deadline = float(doc["openedAt"]) + (float(doc["hours"]) + VERDICT_GRACE_HOURS) * 3600
        while time.time() < deadline:
            item = self.control(doc["stage"])
            shadow = item.get("shadow") or {}
            if item.get("rolloutId") != doc["rolloutId"]:
                raise ExperimentRefused("the stage's rollout is no longer this experiment's")
            if shadow.get("verdict"):
                doc.update(verdict=shadow["verdict"], batchIds=shadow.get("batchIds"), verdictAt=time.time(),
                           scoredPairs=shadow.get("scoredPairs"))
                try:                     # the paired score deltas `shadow report` shows (informational)
                    doc["deltas"] = self.deltas(doc["stage"], doc["rolloutId"], shadow.get("batchIds") or {})
                except Exception as exc:  # noqa: BLE001 - the verdict stands without them
                    _say(f"paired deltas unavailable: {type(exc).__name__}: {str(exc)[:200]}", "yellow")
                self.save(doc)
                _say(f"experiment {xid}: verdict {shadow['verdict']} ({shadow.get('scoredPairs')} scored pairs)", "green")
                return doc
            if should_evaluate(item.get("openedAt", doc["openedAt"]), time.time(), float(doc["hours"]),
                               bool(doc.get("evaluateSentAt"))):
                result = self.invoke(deploy, "shadow-evaluate", {})
                doc["evaluateSentAt"] = time.time()
                self.save(doc)
                _say(f"shadow-evaluate sent: {result.get('note') or result.get('state')}")
            time.sleep(poll)
        raise ExperimentRefused("no verdict within hours + 6 h; run --finish")

    def deltas(self, stage: str, rollout_id: str, batch_ids: dict) -> dict:
        """{evaluator: mean candidate-minus-stable score} over the scored pairs (the controller's own reads)."""
        from .lambdas.controller import handler as H
        ports = H.AwsPorts(env={"M11_STAGE": stage, "OUTCOMES_TABLE": naming.table(stage, "outcomes"),
                                "CONTROL_TABLE": naming.table(stage, "control")},
                           clients={n: self.ctx.admin.client(n) for n in ("dynamodb", "bedrock-agentcore", "logs")})
        item = {"rolloutId": rollout_id, "shadow": {"running": True, "batchIds": batch_ids}}
        obs = ports.shadow_obs(item)
        return {ev: round(sum(v) / len(v), 4) for ev, v in (obs.get("shadowDeltas") or {}).items() if v}

    def finish(self, xid: str) -> dict:
        from .aws import deploy as AD
        from .aws import params
        doc = self.record(xid)
        stage = doc.get("stage") or (params.freeze(self.ssm).get("stage"))
        deploy = AD.Deploy(self.ctx, stage)
        item = self.control(stage)
        steps = finish_steps(item.get("state", "IDLE")) if item.get("rolloutId") == doc.get("rolloutId") else \
            ["reapply", "unfreeze"]
        _say(f"experiment {xid} finish on {stage}: {', '.join(steps)}")
        if "abort" in steps:
            self.invoke(deploy, "abort", {"reason": "experiment done", "latch": False})
        if "wait-rolled-back" in steps:
            end = time.time() + 600
            while self.control(stage).get("state") != "ROLLED_BACK":
                if time.time() > end:
                    raise ExperimentRefused("the abort did not reach ROLLED_BACK in 10 minutes")
                time.sleep(10)
        if "reset" in steps:
            self.invoke(deploy, "reset", {"reason": f"experiment {xid} done"})
        deploy.reapply()
        freeze = params.freeze(self.ssm)
        if freeze.get("reason") == freeze_reason(xid):
            self.set_freeze("none", f"experiment {xid} finished", None)
        doc["finishedAt"] = time.time()
        self.save(doc)
        _say(f"experiment {xid} finished: {stage} back on /current, freeze none", "green")
        return doc


def start_or_restore(exp: "Experiment", source: str, stage: str, hours: float) -> dict:
    """`Experiment.start`; when the start fails after setting its freeze (seal or deploy), the same --finish steps
    restore the stage and clear that freeze before the error is raised, so a failed start never holds a freeze."""
    before = {p.name for p in exp.local.glob("x-*.json")}
    try:
        return exp.start(source, stage, hours)
    except Exception:
        new = sorted((p for p in exp.local.glob("x-*.json") if p.name not in before), key=lambda p: p.stat().st_mtime)
        if new:
            xid = new[-1].stem
            _say(f"experiment {xid} failed to start: restoring {stage} and clearing its freeze", "yellow")
            try:
                exp.finish(xid)
            except Exception as exc:  # noqa: BLE001 - report the original error; the restore can be re-run
                _say(f"restore of {xid} failed ({type(exc).__name__}: {exc}); run `shadow experiment --finish {xid}`",
                     "red")
        raise


def cmd_shadow_experiment(args) -> int:
    exp = Experiment(args.root)
    if args.finish:
        if not EXPERIMENT_ID.match(args.finish):
            raise ValueError("--finish takes an experiment ID x-<16 hex>")
        exp.finish(args.finish)
        return 0
    if not args.source or not args.stage:
        raise ValueError("--source and --stage are required (or --finish <id>)")
    doc = start_or_restore(exp, args.source, args.stage, args.hours)
    if args.name:
        doc = {**exp.record(doc["id"]), "name": args.name}
        exp.save(doc)
    finish_at = float(doc["openedAt"]) + float(doc["hours"]) * 3600
    print(json.dumps({"experiment": doc["id"], "rolloutId": doc["rolloutId"], "manifestVersion": doc["manifestVersion"],
                      "name": args.name, "openedAt": doc["openedAt"], "expectedEvaluateAt": finish_at}))
    if args.wait:
        exp.wait(doc["id"])
    return 0


def cmd_shadow_wait(args) -> int:
    """Send shadow-evaluate after the experiment's --hours and wait for the controller's verdict."""
    if not EXPERIMENT_ID.match(args.id):
        raise ValueError("--wait-for takes an experiment ID x-<16 hex>")
    doc = Experiment(args.root).wait(args.id)
    return 0 if doc.get("verdict") else 1


def cmd_shadow_report(args) -> int:
    """An experiment's record and verdict, by ID or by the --name it was started with (`model-migration`)."""
    from .evaluation import shadow as SH
    doc = SH.find(args.root / "module11/.local/experiments", args.id)
    if doc is None:
        _say(f"no shadow experiment {args.id} recorded in module11/.local/experiments", "yellow")
        return 1
    for line in SH.report_lines(doc):
        _say(line)
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
    return 0 if doc.get("verdict") else 1


def register_pipeline(psub) -> None:
    """`pipeline seal-experiment` (m11-seal-experiment's entry point)."""
    se = psub.add_parser("seal-experiment", help="m11-seal-experiment: seal a branch for the shadow experiment")
    se.set_defaults(fn=cmd_seal_experiment)


def register_shadow(shsub) -> None:
    ex = shsub.add_parser("experiment", help="the audited shadow experiment")
    ex.add_argument("--source", help="branch, tag or 40-hex SHA in the GitLab project")
    ex.add_argument("--stage", choices=list(EXPERIMENT_STAGES))
    ex.add_argument("--hours", type=float, default=24.0, help="shadow duration before the verdict (default 24)")
    ex.add_argument("--wait", action="store_true", help="send shadow-evaluate after --hours and wait for the verdict")
    ex.add_argument("--finish", help="abort (unlatched), reset, reapply and unfreeze experiment <id>")
    ex.add_argument("--name", help="a name `shadow report` finds it by (e.g. model-migration)")
    ex.set_defaults(fn=cmd_shadow_experiment)
    wt = shsub.add_parser("wait", help="send shadow-evaluate after --hours and wait for experiment <id>'s verdict")
    wt.add_argument("id")
    wt.set_defaults(fn=cmd_shadow_wait)
    rp = shsub.add_parser("report", help="an experiment's record and verdict (by ID or name)")
    rp.add_argument("id", help="experiment ID or name (e.g. model-migration)")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_shadow_report)
