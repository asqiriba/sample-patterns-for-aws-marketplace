# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Seed the harness repository with a filtered snapshot of `agentic-ai/`.

Snapshot layout (project-root-relative):
    module10/  module11/  demos/module11_demo.py  tests/test_module11_*  tests/fixtures/module11/
    tests/conftest.py  LICENSE
    module11/gitlab/gitlab-ci.yml -> .gitlab-ci.yml
    module11/gitlab/CODEOWNERS    -> .gitlab/CODEOWNERS   (`@<group>/` rendered from M11_GITLAB_GROUP)
    module11/vercel/vercel.json   -> vercel.json

Only files git tracks (or would track: untracked files that are not ignored) are included, so
module11/.local/, venv links and caches never leave the machine.

Targets:
    --dry-run                 list the snapshot and exit
    --target-dir DIR          write the snapshot into DIR
    --as-bot   (default)      GitLab Commits API commits (<= 200 actions each) on top of the
                              branch head, with the project token read from Secrets Manager;
                              never a force-push or history rewrite
    --as-user                 a signed `git commit -S` and plain `git push` with your own key

Usage (from agentic-ai/):  python -m module11.gitlab.seed --dry-run
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from module11.aws.urls import open_url

AGENTIC_ROOT = Path(__file__).resolve().parents[2]
INCLUDE_PREFIXES = ("module10/", "module11/", "tests/fixtures/module11/")
INCLUDE_FILES = ("demos/module11_demo.py", "tests/conftest.py")
# Earlier modules' code that Module 11 reuses unchanged: Module 9's chunking, quality gates and seed
# corpus (the import closure of module11/corpus/snapshot.py), Module 7's anonymize_pii (path-loaded by
# release/redact.py) and Module 5's guardrail policy source (read with ast by check C11).
REUSED_FILES = ("module5/engine/domain_adapter.py", "module7/memory/guardrails.py", "module9/__init__.py",
                "module9/ingestion/__init__.py", "module9/ingestion/chunk.py", "module9/ingestion/quality.py",
                "module9/mock/__init__.py", "module9/mock/sample_events.py", "module9/mock/seed_corpora_data.py")
INCLUDE_GLOB = "tests/test_module11_"
RENAMES = {"module11/gitlab/gitlab-ci.yml": ".gitlab-ci.yml", "module11/gitlab/CODEOWNERS": ".gitlab/CODEOWNERS",
           "module11/vercel/vercel.json": "vercel.json"}
MAX_ACTIONS = 200
GITLAB_API = "https://gitlab.com/api/v4"


def _git_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                         cwd=root, check=True, capture_output=True).stdout.decode("utf-8")
    return sorted({p for p in out.split("\0") if p and (root / p).is_file() and not (root / p).is_symlink()})


def _included(path: str) -> bool:
    return (path.startswith(INCLUDE_PREFIXES) or path in INCLUDE_FILES or path in REUSED_FILES
            or path.startswith(INCLUDE_GLOB))


def snapshot(root: Path = AGENTIC_ROOT, group: str = "<group>", files: list[str] | None = None) -> list[tuple[str, str]]:
    """[(target path, source path)] for the snapshot; `source` is relative to `root`
    (LICENSE comes from the repository root above agentic-ai/)."""
    pairs = [(RENAMES.get(p, p), p) for p in (files if files is not None else _git_files(root)) if _included(p)]
    licence = root / "LICENSE" if (root / "LICENSE").is_file() else root.parent / "LICENSE"
    if licence.is_file():
        pairs.append(("LICENSE", os.path.relpath(licence, root)))
    return sorted(pairs)


def content(root: Path, source: str, target: str, group: str, quality_users: tuple[str, ...] = ()) -> bytes:
    data = (root / source).read_bytes()
    if target == ".gitlab/CODEOWNERS":
        from module11.release import codeowners
        data = codeowners.render(data.decode("utf-8"), group, quality_users).encode("utf-8")
    return data


def write_dir(root: Path, pairs, out: Path, group: str, quality_users: tuple[str, ...] = ()) -> None:
    for target, source in pairs:
        dest = out / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content(root, source, target, group, quality_users))


def plan_actions(pairs, remote: dict[str, str], read) -> list[dict]:
    """Commits-API actions: create/update for changed files, delete for managed files no longer
    present. `remote` maps path -> blob sha; `read(target)` returns the bytes to upload."""
    import hashlib

    actions = []
    targets = {t for t, _ in pairs}
    for target, _ in pairs:
        data = read(target)
        blob = hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()  # nosemgrep: insecure-hash-algorithm-sha1 - git's blob ID format, not a security use
        if remote.get(target) == blob:
            continue
        actions.append({"action": "update" if target in remote else "create", "file_path": target,
                        "encoding": "base64", "content": base64.b64encode(data).decode("ascii")})
    managed = (tuple(INCLUDE_PREFIXES) + (INCLUDE_GLOB, ".gitlab/", ".gitlab-ci.yml", "vercel.json") + INCLUDE_FILES
               + REUSED_FILES)
    for path in sorted(remote):
        if path not in targets and path.startswith(managed):
            actions.append({"action": "delete", "file_path": path})
    # The CI file and the CI image inputs go last, so the push that adds `.gitlab-ci.yml` also carries the
    # `ci-image` job's `changes:` paths and runs against the complete tree (earlier batches run no pipeline).
    return sorted(actions, key=lambda a: a["file_path"] in CI_LAST or a["file_path"].startswith(CI_LAST_PREFIX))


CI_LAST = (".gitlab-ci.yml", "module11/requirements-ci.txt")
CI_LAST_PREFIX = "module11/gitlab/ci-image/"


def batches(actions: list[dict], size: int = MAX_ACTIONS) -> list[list[dict]]:
    return [actions[i:i + size] for i in range(0, len(actions), size)]


# ---------------------------------------------------------------- GitLab (bot)
def _api(token: str, method: str, path: str, body: dict | None = None):
    request = urllib.request.Request(f"{GITLAB_API}{path}", method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    with open_url(request, timeout=60) as response:
        return json.loads(response.read() or b"null"), response.headers


def _remote_tree(token: str, project: str, branch: str) -> dict[str, str]:
    pid = urllib.parse.quote(project, safe="")
    tree, page = {}, f"/projects/{pid}/repository/tree?recursive=true&per_page=100&pagination=keyset&ref={branch}"
    while page:
        items, headers = _api(token, "GET", page)
        tree.update({i["path"]: i["id"] for i in items if i["type"] == "blob"})
        link = headers.get("Link", "")
        nxt = [p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p]
        page = nxt[0].replace(GITLAB_API, "") if nxt else None
    return tree


def _token() -> str:
    from module11 import aws

    name = os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token")
    return aws.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"].strip()


def seed_as_bot(root: Path, pairs, project: str, branch: str, group: str, message: str,
                quality_users: tuple[str, ...] = ()) -> int:
    token = _token()  # held in memory only; never printed or written
    remote = _remote_tree(token, project, branch)
    actions = plan_actions(pairs, remote, lambda t: content(root, dict(pairs)[t], t, group, quality_users))
    if not actions:
        print("Remote already matches the snapshot; nothing to commit.")
        return 0
    pid = urllib.parse.quote(project, safe="")
    chunks = batches(actions)
    for n, chunk in enumerate(chunks, start=1):
        suffix = f" ({n}/{len(chunks)})" if len(chunks) > 1 else ""
        commit, _ = _api(token, "POST", f"/projects/{pid}/repository/commits",
                         {"branch": branch, "commit_message": message + suffix, "actions": chunk})
        print(f"Committed {len(chunk)} actions: {commit['id'][:12]}{suffix} (signature: {_signature(token, pid, commit['id'])})")
    return 0


def _signature(token: str, pid: str, sha: str) -> str:
    """The commit's GitLab signature status: verified, unverified, or none."""
    try:
        doc, _ = _api(token, "GET", f"/projects/{pid}/repository/commits/{sha}/signature")
    except urllib.error.HTTPError as exc:
        return "none" if exc.code == 404 else f"unknown ({exc.code})"
    return f"{doc.get('signature_type', '?')} {doc.get('verification_status', '?')}"


# ---------------------------------------------------------------- git push (user)
def seed_with_git(root: Path, pairs, remote_url: str, branch: str, group: str, message: str, sign: bool,
                  git_config: list[str] | None = None, quality_users: tuple[str, ...] = ()) -> int:
    cfg = sum((["-c", c] for c in (git_config or [])), [])
    with tempfile.TemporaryDirectory(prefix="m11-seed-") as tmp:
        work = Path(tmp) / "repo"
        subprocess.run(["git", *cfg, "clone", "--quiet", remote_url, str(work)], check=True)
        exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"origin/{branch}"], cwd=work,
                                capture_output=True).returncode == 0
        subprocess.run(["git", "checkout", "--quiet", *([branch] if exists else ["-b", branch])], cwd=work, check=True)
        for entry in work.iterdir():
            if entry.name != ".git" and entry.name != "README.md":
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        write_dir(root, pairs, work, group, quality_users)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=work).returncode == 0:
            print("Remote already matches the snapshot; nothing to commit.")
            return 0
        subprocess.run(["git", "commit", "--quiet", *(["-S"] if sign else []), "-m", message], cwd=work, check=True)
        subprocess.run(["git", *cfg, "push", "--quiet", "origin", f"HEAD:{branch}"], cwd=work, check=True)
    print(f"Pushed the snapshot to {branch}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-dir")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--as-bot", action="store_true", help="GitLab Commits API with the project token (default)")
    mode.add_argument("--as-user", action="store_true", help="signed git push with your own key")
    parser.add_argument("--project", default=os.environ.get("M11_GITLAB_PROJECT", ""))
    parser.add_argument("--branch", default="main")
    parser.add_argument("--group", default=os.environ.get("M11_GITLAB_GROUP", ""),
                        help="the GitLab group that holds harness-owners, platform and quality "
                             "(default M11_GITLAB_GROUP, else the group part of --project)")
    parser.add_argument("--quality-users", default=os.environ.get("M11_QUALITY_USERS", ""),
                        help="comma-separated GitLab usernames for the [Quality] code-owner section (default "
                             "M11_QUALITY_USERS); without them the section names the quality group, and GitLab may "
                             "count an inherited group Owner as eligible")
    parser.add_argument("--message", default="Seed the DevOps Companion harness (Module 11)")
    args = parser.parse_args(argv)
    quality = tuple(u.strip() for u in args.quality_users.split(",") if u.strip())
    if not args.group and "/" in args.project:
        args.group = args.project.rpartition("/")[0]           # the project's group: <group>/<project>
    if not args.group and args.dry_run:
        args.group = "<group>"                                 # a listing only: CODEOWNERS stays unrendered
    if not args.group:
        parser.error("--group (or M11_GITLAB_GROUP, or --project <group>/<project>) is required")

    root = AGENTIC_ROOT
    pairs = snapshot(root, args.group)
    if args.dry_run:
        for target, source in pairs:
            print(target if target == source else f"{target}  <-  {source}")
        print(f"{len(pairs)} files")
        return 0
    if not quality:
        print("warning: no --quality-users; the [Quality] section names the quality group (an Owner inherited from "
              "the parent group may be eligible for it)", file=sys.stderr)
    if args.target_dir:
        write_dir(root, pairs, Path(args.target_dir), args.group, quality)
        print(f"Wrote {len(pairs)} files to {args.target_dir}")
        return 0
    if not args.project:
        parser.error("--project (or M11_GITLAB_PROJECT) is required")
    if args.as_user:
        return seed_with_git(root, pairs, f"git@gitlab.com:{args.project}.git", args.branch, args.group,
                             args.message, sign=True, quality_users=quality)
    return seed_as_bot(root, pairs, args.project, args.branch, args.group, args.message, quality)


if __name__ == "__main__":
    sys.exit(main())
