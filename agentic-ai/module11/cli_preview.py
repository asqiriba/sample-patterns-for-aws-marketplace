# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The trusted preview builds: main's tooling on an MR's data, every GitLab call through
the m11-gitlab-notes broker (no GitLab token in any preview project).

  pipeline preview --exports F       m11-preview: PREVIEW_ACTION deploy | gate | destroy for MR PREVIEW_IID at
                                     PREVIEW_SHA (validated before use, never echoed)
  pipeline preview-infra-verify      m11-preview-infra's checks before any MR tooling runs: the job is
                                     `preview:deploy-infra`, running, in a pipeline for the MR at its SHA, and its
                                     review-infra deployment was approved by a `platform` member; then the MR tree
                                     is unpacked at /tmp/mr for the MR's own tooling
  pipeline bot-commit --change ID    m11-bot (fallback): a signed bot commit of a prepared change set

The steps: 1 validate; 2 MR checks (targets main, not a fork; deploy/gate: open at PREVIEW_SHA;
destroy: closed/merged or its environment stopping/stopped, else PreviewRejected); 3 the commit archive
(previews/archives/<sha>.zip, one per SHA); 4 split main's tooling from the MR's data (release/preview_inputs.py);
5 the image only when runtime/code changed (m11-preview-image); 6 `release deploy --stage mr-<iid>`, the fast gate
and the MR note.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .release import naming

IID = re.compile(r"^\d{1,9}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
JOB = re.compile(r"^\d{1,12}$")
ACTIONS = ("deploy", "gate", "destroy")
MARKER = "<!-- m11-gate -->"


class PreviewRejected(RuntimeError):
    pass


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def inputs(env: dict) -> tuple[str, int, str]:
    action, iid, sha = env.get("PREVIEW_ACTION", ""), env.get("PREVIEW_IID", ""), env.get("PREVIEW_SHA", "")
    if action not in ACTIONS or not IID.match(iid) or not SHA.match(sha):
        raise PreviewRejected("invalid input")
    return action, int(iid), sha


def check_mr(mr: dict, action: str, sha: str, environment: dict | None = None) -> None:
    """Preview step 2. `mr` is the broker's merge_request result; `environment` its environment result."""
    if mr.get("targetBranch") != "main":
        raise PreviewRejected("the MR does not target main")
    if mr.get("sourceProjectId") != mr.get("targetProjectId"):
        raise PreviewRejected("fork MRs are not previewed")
    if action == "destroy":
        stopped = (environment or {}).get("state") in ("stopping", "stopped")
        if mr.get("state") not in ("closed", "merged") and not stopped:
            raise PreviewRejected("destroy of an open MR's live environment")
        return
    if mr.get("state") != "opened":
        raise PreviewRejected(f"the MR is {mr.get('state')}")
    if mr.get("sha") != sha:
        raise PreviewRejected("PREVIEW_SHA is not the MR's head")


def check_infra_job(job: dict, deployment: dict, *, iid: int, sha: str, members: dict[int, bool]) -> None:
    """The job and its review-infra approval, before any MR code runs."""
    pipe = job.get("pipeline") or {}
    if job.get("name") != "preview:deploy-infra" or job.get("status") != "running":
        raise PreviewRejected("not a running preview:deploy-infra job")
    if pipe.get("sha") != sha or pipe.get("ref") not in (f"refs/merge-requests/{iid}/head",
                                                         f"refs/merge-requests/{iid}/merge"):
        raise PreviewRejected("the job belongs to another MR or commit")
    approved = [a for a in deployment.get("approvals") or [] if a.get("status") == "approved"]
    if not deployment.get("found") or not any(members.get(int(a["userId"])) for a in approved if a.get("userId")):
        raise PreviewRejected("review-infra has no approval by a platform member")


def merge_tree(tooling: Path, mr: Path, out: Path) -> dict:
    """main's tooling with the MR's harness data (preview_inputs.split): the gate definition stays main's."""
    from .release import preview_inputs as PI
    split = PI.split(PI.read_tree(tooling), PI.read_tree(mr))
    shutil.copytree(tooling, out, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "venv"))
    harness = out / "module11/harness"
    for path in sorted(harness.rglob("*")):
        rel = path.relative_to(out).as_posix()
        if path.is_file() and not rel.startswith(PI.EVALUATION):
            path.unlink()                         # only the MR's harness files (outside evaluation/) remain
    for rel, data in split.harness.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {"proposed": sorted(split.proposed), "removed": split.removed,
            "changedScenarios": split.changed_scenarios()}


def chain_through_deployer(ctx, stage: str) -> None:
    """The MR gate reaches its mr-<iid> stage as the deploy does: through m11-preview-deployer. That session
    reads mr-<iid>/current and assumes the stack's eval runner, which trusts only the operators and the preview
    deployer. No IAM grant or trust is widened for it."""
    ctx.via[stage] = ctx.assume(naming.deployer_role(stage), stage, f"preview gate {stage}")


def _notes(ctx, op: dict) -> dict:
    from .cli_pipeline import _notes as call
    body = call(ctx, op)
    if not body.get("ok"):
        raise PreviewRejected(f"broker {op.get('operation')}: {body.get('error')}")
    return body["result"]


def cmd_preview(args) -> int:
    from .aws import deploy as AD
    from .aws import releases
    from .cli_experiment import extract_archive, utc_z
    from .cli_live import _context
    from .cli_pipeline import write_exports
    action, iid, sha = inputs(os.environ)
    stage = f"mr-{iid}"
    ctx = _context(args.root)
    provenance = f"tooling: main@{tooling_commit(ctx)[:12]} · harness inputs: !{iid}@{sha[:12]}"
    _say(provenance)                     # the build log's first line (the MR job cannot read build logs)
    mr = _notes(ctx, {"operation": "merge_request", "iid": iid})
    env = _notes(ctx, {"operation": "environment", "name": f"review/{stage}"}) if action == "destroy" else None
    check_mr(mr, action, sha, env)
    if action == "destroy":
        AD.Deploy(ctx, stage).destroy()
        _say(f"destroyed {stage}", "green")
        return 0
    s3 = ctx.admin.client("s3")
    archive = _notes(ctx, {"operation": "archive", "sha": sha})
    data = releases.get_verified(s3, f"s3://{ctx.release_bucket}/{archive['key']}", "sha256:" + archive["sha256"],
                                 "MR archive")
    commit = _notes(ctx, {"operation": "commit", "ref": sha})
    with tempfile.TemporaryDirectory(prefix="m11-preview-") as tmp:
        mr_tree = extract_archive(data, Path(tmp) / "mr")
        merged = Path(tmp) / "merged"
        info = merge_tree(args.root, mr_tree, merged)
        git = {"commit": sha, "timestamp": utc_z(commit["createdAt"]), "tag": None, "subject": commit.get("title") or ""}
        if action == "gate":
            from .cli_eval import EXIT_OVER_BUDGET, cmd_gate
            from .cli_pipeline import _Args
            chain_through_deployer(ctx, stage)
            gate_args = _Args(root=merged, stage=stage, profile="preview", scope=None, scenario=None,
                              from_run=None, no_write=False, max_usd=25.0, warmup=None, ctx=ctx)
            code = cmd_gate(gate_args)
            verdict = {0: "PASS", 2: "FAIL", 3: "INCONCLUSIVE", EXIT_OVER_BUDGET: "OVER BUDGET"}.get(code, f"exit {code}")
            note = (f"**m11 preview gate: {verdict}** for `{sha[:12]}` on `{stage}` (main's gate definition).\n\n"
                    f"`{provenance}`")
            report = getattr(gate_args, "report", None)
            if report:
                note += "\n\n" + report["markdown"]
            if info["proposed"] or info["removed"]:
                note += ("\n\nThis MR changes evaluation files; they were not used by this gate (informational: "
                         f"{', '.join(info['changedScenarios']) or 'no scenario changes'}).")
            from .aws import ui_wiring
            note += "\n\n" + ui_wiring.note_line(ui_record(ctx, iid, sha))
            _notes(ctx, {"operation": "mr_note", "iid": iid, "body": note + f"\n\n{MARKER}"})
            write_exports(args.exports, {"GATE_EXIT": code})
            return code
        mv = preview_seal(ctx, merged, git, stage)
        result = AD.Deploy(ctx, stage, ui=ui_wiring_for(ctx, iid, sha, mr, commit)).run(mv)
    ui = result.get("ui")
    if ui:
        releases.put_json(s3, ctx.release_bucket, f"previews/{iid}/{sha}.ui.json", ui)
        _say(ui.get("note", ""))
    url = (ui or {}).get("origin") if (ui or {}).get("action") == "wire" else None
    write_exports(args.exports, {"PREVIEW_URL": url or result.get("apiUrl") or "", "GATE_EXIT": 0})
    _say(f"deployed {mv} to {stage} ({result['path']}) in {result['seconds']} s", "green")
    return 0


def tooling_commit(ctx) -> str:
    from .aws import releases
    pointer = json.loads(releases.get(ctx.admin.client("s3"), f"s3://{ctx.release_bucket}/tooling/main.json"))
    return str(pointer.get("commit") or "")


def ui_wiring_for(ctx, iid: int, sha: str, mr: dict, commit: dict):
    """The UI-preview inputs, all through the broker and Secrets Manager (no GitLab token here)."""
    from .aws import params, ui_wiring
    shared = params.shared_outputs(ctx.admin.client("ssm"))
    head = ui_wiring.MrHead(iid=iid, branch=mr["sourceBranch"], sha=sha,
                            author_username=str(mr.get("authorUsername") or ""),
                            commit_author_email=str(commit.get("authorEmail") or ""))
    return ui_wiring.for_preview(ctx.admin, head, shared, gitlab_project_id=os.environ.get("M11_GITLAB_PROJECT_ID", ""),
                                 log=lambda line: _say(line))


def ui_record(ctx, iid: int, sha: str) -> dict | None:
    from .aws import releases
    try:
        return json.loads(releases.get(ctx.admin.client("s3"), f"s3://{ctx.release_bucket}/previews/{iid}/{sha}.ui.json"))
    except Exception:  # noqa: BLE001 - no record: the deploy predates the wiring, or skipped writing it
        return None


def preview_seal(ctx, tree: Path, git: dict, stage: str) -> str:
    """Seal the merged tree for an mr-* stack (content-addressed objects; the image only if runtime/code changed)."""
    from .aws import params, releases
    from .release import detect as DT
    from .release import manifest as M
    from .release import seal as SEAL
    from .release import stage as cfg
    from .release.hashing import hex_of
    s3 = ctx.admin.client("s3")
    pointer = json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/tooling/main.json"))
    main = json.loads(releases.get(s3, f"s3://{ctx.release_bucket}/tooling/{pointer['commit']}.manifest.json"))
    declared = M.build_declared(tree, git)
    rules, unknown, _ = cfg.change_types(tree)
    plan = DT.detect(M.hash_set(declared), M.hash_set(main), rules, unknown,
                     base_manifest_version=main["manifestVersion"], new_tools=declared.get("tools"),
                     base_tools=main.get("tools"))
    prod = params.stage_get(ctx.admin.client("ssm"), "prod", "current")
    prod_full = json.loads(releases.get(s3, prod["s3Uri"])) if prod else {}
    context_hex = hex_of(declared["build"]["imageContext"])
    if prod_full and declared["build"]["imageContext"] == prod_full["build"]["imageContext"]:
        digest = prod_full["build"]["imageDigest"]
    else:
        digest = preview_image(ctx, tree, stage, context_hex)
    layer = SEAL.build_layer(tree / "module11/requirements-lambda.txt")
    ops_layer = SEAL.build_layer(tree / SEAL.OPS_REQUIREMENTS) if SEAL.has_operations(tree) else None
    objects, record, _ = SEAL.seal_objects(tree, declared, image_digest=digest, layer_zip=layer,
                                           change_plan=plan.to_dict(), sealed_by="m11-preview",
                                           sealed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                           ops_layer_zip=ops_layer)
    releases.upload(s3, ctx.release_bucket, objects)
    mv = declared["manifestVersion"]
    releases.put_absent(s3, ctx.release_bucket, f"releases/{mv}/seal.json",
                        json.dumps(record, indent=2, sort_keys=True).encode("utf-8"), content_type="application/json")
    return mv


def preview_image(ctx, tree: Path, stage: str, context_hex: str) -> str:
    """Preview step 5: the MR's context goes to m11-preview-image (no cache, no secret, no deployer)."""
    from .aws import releases
    from .release import seal as SEAL
    iid = stage.split("-", 1)[1]
    s3, cb, ecr = ctx.admin.client("s3"), ctx.admin.client("codebuild"), ctx.admin.client("ecr")
    releases.put_absent(s3, ctx.release_bucket, f"previews/{iid}/{context_hex}.zip", SEAL.image_context(tree),
                        content_type="application/zip")
    tag = f"mr-{iid}-{context_hex[:16]}"
    build = cb.start_build(projectName="m11-preview-image", environmentVariablesOverride=[
        {"name": "PREVIEW_IID", "value": iid, "type": "PLAINTEXT"},
        {"name": "CONTEXT_HASH", "value": context_hex, "type": "PLAINTEXT"}])["build"]
    while True:
        time.sleep(10)
        status = cb.batch_get_builds(ids=[build["id"]])["builds"][0]["buildStatus"]
        if status != "IN_PROGRESS":
            break
    if status != "SUCCEEDED":
        raise PreviewRejected(f"m11-preview-image ended {status}")
    digest = releases.image_digest(ecr, naming.PREVIEW_IMAGE_REPOSITORY, tag)
    if not digest:
        raise PreviewRejected("the preview image is missing after its build")
    return digest


def _gitlab_group() -> str:
    """The top-level GitLab group: M11_GITLAB_GROUP, else the group part of M11_GITLAB_PROJECT (<group>/<project>)."""
    group = os.environ.get("M11_GITLAB_GROUP") or os.environ.get("M11_GITLAB_PROJECT", "").rpartition("/")[0]
    if not group:
        raise PreviewRejected("M11_GITLAB_GROUP or M11_GITLAB_PROJECT is required")
    return group


def cmd_preview_infra_verify(args) -> int:
    from .aws import releases
    from .cli_experiment import extract_archive
    from .cli_live import _context
    _, iid, sha = inputs({**os.environ, "PREVIEW_ACTION": "deploy"})
    job_id = os.environ.get("PREVIEW_JOB_ID", "")
    if not JOB.match(job_id):
        raise PreviewRejected("invalid input")
    ctx = _context(args.root)
    check_mr(_notes(ctx, {"operation": "merge_request", "iid": iid}), "deploy", sha)
    job = _notes(ctx, {"operation": "job", "job_id": int(job_id)})
    deployment = _notes(ctx, {"operation": "deployment", "environment": "review-infra", "job_id": int(job_id)})
    members = {}
    for a in deployment.get("approvals") or []:
        if a.get("userId"):
            m = _notes(ctx, {"operation": "group_member", "group": f"{_gitlab_group()}/platform",
                             "user_id": a["userId"]})
            members[int(a["userId"])] = bool(m.get("member"))
    check_infra_job(job, deployment, iid=iid, sha=sha, members=members)
    archive = _notes(ctx, {"operation": "archive", "sha": sha})
    data = releases.get_verified(ctx.admin.client("s3"), f"s3://{ctx.release_bucket}/{archive['key']}",
                                 "sha256:" + archive["sha256"], "MR archive")
    tree = extract_archive(data, Path(tempfile.mkdtemp(prefix="mr-archive-")))   # private 0700 directory
    shutil.copytree(tree, "/tmp/mr", dirs_exist_ok=True)   # nosec B108 - the buildspec's fixed path, ephemeral build container
    _say(f"preview:deploy-infra verified for MR !{iid} at {sha[:12]} (job {job_id})", "green")
    return 0


CHANGE_ID = re.compile(r"^[0-9a-f]{16}$")
# A GitLab project path, <group>[/<subgroup>...]/<project>: GitLab's path characters, no leading '-' or '.'.
PROJECT_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}){1,20}$")
AUTHOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,99}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
MAX_MESSAGE = 10_000


def gitlab_project(value: str) -> str:
    """M11_GITLAB_PROJECT, checked before it becomes part of the clone URL."""
    if not PROJECT_PATH.fullmatch(value or "") or ".." in value:
        raise ValueError("M11_GITLAB_PROJECT must be a GitLab project path such as <group>/<project>")
    return value


def repo_path(path: object) -> str:
    """A repository-relative POSIX file path: not absolute, no '.', '..' or empty part, nothing under .git/."""
    from pathlib import PurePosixPath
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise ValueError("bot change paths must be repository-relative file paths")
    parts = PurePosixPath(path).parts
    if (path.startswith("/") or any(p in ("", ".", "..") for p in path.split("/"))
            or parts[0].lower() == ".git"):
        raise ValueError("bot change paths must be repository-relative file paths")
    return path


def bot_change(change: object) -> dict:
    """The bot change set, checked before any of it reaches git or the file system: a bot branch, repository-
    relative file paths with base64 content, a one-line author name, an email address and a text message."""
    from .aws import bot_changes
    if not isinstance(change, dict):
        raise ValueError("invalid bot change")
    branch = change.get("branch")
    if (not isinstance(branch, str) or not bot_changes.BOT_BRANCH.fullmatch(branch) or ".." in branch
            or branch.endswith("/")):
        raise ValueError("bot branches only")
    files = change.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("a bot change needs at least one file")
    for path, data in files.items():
        repo_path(path)
        if not isinstance(data, str):
            raise ValueError("bot change file content must be base64 text")
    author = change.get("author", "m11 bot")
    if not isinstance(author, str) or not AUTHOR.fullmatch(author):
        raise ValueError("invalid bot change author")
    email = change.get("email", "m11-bot@noreply.gitlab.com")
    if not isinstance(email, str) or not EMAIL.fullmatch(email):
        raise ValueError("invalid bot change email")
    message = change.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE or "\x00" in message:
        raise ValueError("invalid bot change message")
    return {"branch": branch, "files": files, "author": author, "email": email, "message": message}


def cmd_bot_commit(args) -> int:
    """m11-bot: apply bot/<change>.json (branch, message, {path: base64}) as a signed commit and push the branch.
    Every value that reaches git is checked first (change ID, project path, branch, paths, author, email, message);
    git runs with list arguments and no shell."""
    import base64
    from .aws import releases
    from .cli_live import _context
    if not CHANGE_ID.fullmatch(args.change):
        raise ValueError("invalid change ID")
    project = gitlab_project(os.environ.get("M11_GITLAB_PROJECT", ""))
    ctx = _context(args.root)
    sm = ctx.admin.client("secretsmanager")
    change = bot_change(json.loads(releases.get(ctx.admin.client("s3"),
                                                f"s3://{ctx.release_bucket}/bot/{args.change}.json")))
    branch = change["branch"]
    token = sm.get_secret_value(SecretId=os.environ.get("M11_GITLAB_TOKEN_SECRET",
                                                        "devops-companion/partners/gitlab-token"))["SecretString"].strip()
    key = sm.get_secret_value(SecretId="devops-companion/partners/gitlab-bot-signing-key")["SecretString"]
    with tempfile.TemporaryDirectory(prefix="m11-bot-") as tmp:
        os.chmod(tmp, 0o700)  # nosemgrep: insecure-file-permissions - owner-only: the directory holds the key and token helper
        keyfile = Path(tmp) / "signing"
        keyfile.write_text(key)
        keyfile.chmod(0o600)
        helper = Path(tmp) / "cred.sh"
        helper.write_text("#!/bin/sh\n[ \"$1\" = get ] && printf 'username=oauth2\\npassword=%s\\n' \"$M11_BOT_TOKEN\"\n")
        helper.chmod(0o700)
        env = {**os.environ, "M11_BOT_TOKEN": token}
        repo = Path(tmp) / "repo"
        url = f"https://gitlab.com/{project}.git"
        git = ["git", "-c", f"credential.helper={helper}", "-c", "gpg.format=ssh", "-c", f"user.signingkey={keyfile}"]
        # nosemgrep: dangerous-subprocess-use-tainted-env-args, dangerous-subprocess-use-audit - list args, no shell; project checked by gitlab_project()
        subprocess.run([*git, "clone", "--quiet", "--depth", "20", url, str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-B", branch], check=True)
        root = repo.resolve()
        for path, b64 in sorted(change["files"].items()):
            target = (repo / path).resolve()
            if not target.is_relative_to(root) or target == root:   # a symlink in the clone cannot redirect a write
                raise ValueError("bot change paths must stay inside the repository")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(b64, validate=True))
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; author, email and message checked by bot_change()
        subprocess.run([*git[:1], "-C", str(repo), *git[1:], "-c", f"user.name={change['author']}",
                        "-c", f"user.email={change['email']}", "commit", "-q", "-S",
                        "-m", change["message"]], check=True, env=env)
        # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; branch checked by bot_change()
        subprocess.run([*git[:1], "-C", str(repo), *git[1:], "push", "-q", "origin", f"HEAD:{branch}"], check=True,
                       env=env)
    del token, key
    _say(f"bot pushed a signed commit to {branch}", "green")
    return 0


def register_pipeline(psub) -> None:
    pv = psub.add_parser("preview", help="m11-preview: deploy, gate or destroy one MR's preview stack")
    pv.add_argument("--exports")
    pv.set_defaults(fn=cmd_preview)
    pi = psub.add_parser("preview-infra-verify", help="m11-preview-infra's checks before the MR's tooling runs")
    pi.set_defaults(fn=cmd_preview_infra_verify)
    bc = psub.add_parser("bot-commit", help="m11-bot: a signed bot commit of bot/<change>.json")
    bc.add_argument("--change", required=True)
    bc.set_defaults(fn=cmd_bot_commit)
