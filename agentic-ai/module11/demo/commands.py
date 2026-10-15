# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live bindings and argparse wiring for `module11 demo arm|rearm` and `demo prep --reset|--payments|--warm`.
The command logic lives in demo/rearm.py, demo/arm.py and demo/prep.py, which take ports so the
offline tests run them against fakes; this module builds the real ports from the operator's own session.
Both commands use the operator's identity; no new role is needed."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

LOG = logging.getLogger("module11.demo")


def _say(text: str, style: str | None = None) -> None:
    from module11.cli import say
    say(text, style)


def _log(level: str, line: str) -> None:
    style = {"ERROR": "red", "WARN": "yellow"}.get(level)
    _say(f"{level} {line}" if level in ("ERROR", "WARN") else line, style)
    LOG.log(logging.ERROR if level == "ERROR" else logging.WARNING if level == "WARN" else logging.INFO, line)


def reason_arg(text: str) -> str:
    if not 1 <= len(text) <= 200 or not text.isprintable():
        raise argparse.ArgumentTypeError("--reason must be 1-200 printable characters")
    return text


def _alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def stack_output(ctx, stage: str, key: str):
    from module11.aws import deploy as AD
    return AD.Deploy(ctx, stage).outputs().get(key)


def _ports_common(ctx, stage: str):
    """The fields demo rearm and demo arm share (rearm.Ports)."""
    from module11.aws import control_table, ledger, memory, params, releases
    from module11.cli_ops import _invoke, _memory_id, _side_effect_keys
    from module11.demo import state as S
    from module11.release import audit, latch, naming
    ddb, ssm, s3 = ctx.admin.client("dynamodb"), ctx.admin.client("ssm"), ctx.admin.client("s3")
    table = naming.table(stage, "control")

    def latch_clear(digest: str, reason: str) -> None:
        """Audited, and a no-op when the digest is not latched (so a re-run never audits twice)."""
        name = latch.name(digest)
        try:
            before = json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
        except ssm.exceptions.ParameterNotFound:
            return
        if before.get("state") != "latched":
            return
        actor = ctx.admin.client("sts").get_caller_identity()["Arn"]
        value, _ = latch.cleared(digest, reason, actor, time.time())
        ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True, Tier="Standard")
        ledger.put(s3, ctx.release_bucket, audit.SHARED, "latch-clear", by=actor, parameter=name,
                   behaviorDigest=digest, reason=reason, rolloutId=before.get("rolloutId"),
                   previous=before.get("reason"))

    def flags_restore() -> None:
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI with a fixed stage (prod or dev)
        code = subprocess.run([sys.executable, "-m", "module11", "flags", "restore", "--stage", stage],
                              cwd=ctx.root).returncode
        if code != 0:
            raise RuntimeError(f"flags restore --stage {stage} exited {code}")

    def candidate_pins(item) -> dict:
        mv = item.get("manifestVersion")
        return json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/release-inputs/{stage}/{mv}.pins.json"))

    def delete_records(release: str) -> bool:
        _, _, notes = _side_effect_keys(ctx, stage, release)
        key = memory.purge_record_key(stage, release)
        try:
            before = json.loads(s3.get_object(Bucket=ctx.release_bucket, Key=key)["Body"].read())
        except s3.exceptions.NoSuchKey:
            before = {}
        report = memory.purge(_memory_id(ctx, stage), stage=stage, release=release, dry_run=False,
                              data_plane=ctx.admin.client("bedrock-agentcore"), notes=notes,
                              removed_before=dict(before.get("removed") or {}), log=lambda line: _say(line))
        from datetime import datetime, timezone
        releases.put_json(s3, ctx.release_bucket, key, memory.purge_record(stage, release, report,
                                                                           datetime.now(timezone.utc)))
        _say(f"memory purge: deleted {report.deleted} of {len(notes)} note(s), failed {len(report.failed)}")
        return report.complete and not report.failed

    def write_audit(kind: str, fields: dict) -> str:
        # The record's own `stage` field is the ledger stage (`demo arm` passes it; ledger.put takes it once).
        fields = {k: v for k, v in fields.items() if k != "stage"}
        key, _ = ledger.put(s3, ctx.release_bucket, stage, kind, by=fields.get("caller") or "operator", **fields)
        return key

    return dict(
        stage=stage, state_path=S.path_of(), rollout_profile=lambda: str(stack_output(ctx, stage, "rolloutProfile")),
        caller_arn=lambda: ctx.admin.client("sts").get_caller_identity()["Arn"],
        operator_principals=list(ctx.operators), read_control=lambda: control_table.get_raw(ddb, table),
        controller=lambda command, args: _invoke(ctx, stage, command, args), latch_clear=latch_clear,
        flags_restore=flags_restore, candidate_pins=candidate_pins, delete_records=delete_records,
        audit=write_audit, alive=_alive, killpg=_killpg, clock=time.time, sleep=time.sleep, log=_log)


# ---------------------------------------------------------------- demo rearm
def cmd_demo_rearm(args) -> int:
    from module11.cli_live import _context
    from module11.demo import live_check, rearm as R
    ctx = _context(args.root)
    ports = R.Ports(**_ports_common(ctx, "prod"), pipeline=ctx.admin.client("codepipeline"),
                    live_check_armed=lambda: live_check.main(["--demo", "--armed"]) == 0)
    return R.rearm(ports, args.reason)


# ---------------------------------------------------------------- demo arm --stage dev
def bad_manifest_digest(root: Path) -> str:
    """Behavior digest of HEAD with PAYMENTS_TWEAK applied, computed in a temporary export (no git writes)."""
    from module11.cli import git_info, tree_at
    from module11.demo import wording
    from module11.release import manifest as M
    with tree_at(root, "HEAD") as tree:
        prompt = tree / wording.COMPANION_PROMPT
        prompt.write_text(wording.apply_edit(prompt.read_text(encoding="utf-8"), wording.PAYMENTS_TWEAK),
                          encoding="utf-8")
        return M.build_declared(tree, git_info(root))["digests"]["behavior"]


def deploy_bad_to_dev(root: Path, ctx) -> dict:
    """Seed a local branch with PAYMENTS_TWEAK (a temporary worktree, as test_live_controller does),
    seal it as the operator and `release deploy --stage dev`; returns the control item the deploy opened."""
    import re
    import tempfile
    from module11.aws import control_table
    from module11.demo import wording
    from module11.release import naming
    branch = "m11-seeded/payments-tweak"
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                         check=True).stdout.strip()
    prefix = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-prefix"], capture_output=True, text=True,
                            check=True).stdout.strip()
    path = f"{prefix}{wording.COMPANION_PROMPT}"
    git = lambda *a, cwd=top: subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,  # noqa: E731
                                             text=True).stdout.strip()
    if subprocess.run(["git", "-C", top, "rev-parse", "--verify", "-q", branch], capture_output=True).returncode == 0:
        git("branch", "-D", branch)
    with tempfile.TemporaryDirectory(prefix="m11-arm-") as tmp:
        wt = Path(tmp) / "wt"
        git("worktree", "add", "-q", "--no-checkout", "-b", branch, str(wt), "HEAD")
        try:
            git("sparse-checkout", "set", "--no-cone", path, cwd=wt)
            git("checkout", "-q", branch, cwd=wt)
            target = wt / path
            target.write_text(wording.apply_edit(target.read_text(encoding="utf-8"), wording.PAYMENTS_TWEAK),
                              encoding="utf-8")
            git("add", path, cwd=wt)
            git("commit", "-q", "-m", "demo arm: the payments team's edit (PAYMENTS_TWEAK), dev rehearsal only", cwd=wt)
        finally:
            git("worktree", "remove", "--force", str(wt))
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI: fixed subcommands, the seeded branch, its sealed version
    run = lambda *a: subprocess.run([sys.executable, "-m", "module11", *a], cwd=root,  # noqa: E731
                                    capture_output=True, text=True)
    sealed = run("release", "seal", "--operator", "--ref", branch)
    match = re.search(r"^sealed (\S+): ", sealed.stdout, re.M)
    if sealed.returncode != 0 or not match:
        raise RuntimeError(f"seal failed: {(sealed.stdout + sealed.stderr)[-800:]}")
    deployed = run("release", "deploy", "--stage", "dev", "--manifest", match.group(1))
    if deployed.returncode != 0:
        raise RuntimeError(f"release deploy failed: {(deployed.stdout + deployed.stderr)[-800:]}")
    item = control_table.get_raw(ctx.admin.client("dynamodb"), naming.table("dev", "control")) or {}
    if item.get("state") not in ("WARMING", "OBSERVING"):
        raise RuntimeError(f"the deploy did not open a rollout (dev control item {item.get('state')})")
    return item


def cmd_demo_arm(args) -> int:
    from module11.aws import params
    from module11.cli_live import _context
    from module11.demo import arm as A
    ctx = _context(args.root)
    ssm = ctx.admin.client("ssm")
    bad = bad_manifest_digest(args.root)

    def current_digest():
        current = params.stage_get(ssm, "dev", "current")
        return ((current or {}).get("digests") or {}).get("behavior")

    def latched(digest: str) -> bool:
        return (params.latched(ssm, digest) or {}).get("state") == "latched"
    ports = A.ArmPorts(**_ports_common(ctx, "dev"), bad_digest=lambda: bad, current_digest=current_digest,
                       latched=latched, deploy_bad=lambda: deploy_bad_to_dev(args.root, ctx))
    return A.arm(ports, rearm=args.rearm, reason=args.reason)


def register(dsub) -> None:
    arm = dsub.add_parser("arm", help="deploy the deliberately bad payments release to dev only (canary rehearsal)")
    arm.add_argument("--stage", required=True, choices=["dev"], help="dev only; prod and staging are refused")
    arm.add_argument("--rearm", action="store_true", help="stop, abort, clean up and reset a previous dev rehearsal")
    arm.add_argument("--reason", type=reason_arg, default="demo dry run")
    arm.set_defaults(fn=cmd_demo_arm)
    re_ = dsub.add_parser("rearm", help="re-run the armed release's Production stage for a B5 re-take")
    re_.add_argument("--reason", type=reason_arg, default="demo retake")
    re_.set_defaults(fn=cmd_demo_rearm)
