# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bot change requests on GitLab: branch + commit + MR by the bot, never a push to main.

One idempotence rule for every bot-opened MR: the branch name is deterministic; an open MR for it is
reused (its iid is returned); a leftover branch with no open MR is deleted and recreated. Bot branches start with
`demo/`, `corpus/`, `recommendation/`, `scenario/` or `model/` (a model-migration branch), which vercel.json excludes from git deployments, so a bot
change never builds a UI preview. Commits use the Commits API, so GitLab signs them when web-based commit
signing is on.

`open_change` never asks GitLab to merge on its own (no merge-when-pipeline-succeeds): GitLab's mergeable flag can
count an inherited group Owner for a group code-owner section. `wait_and_merge` merges only when the pipeline
passed, GitLab reports the MR approved, and `module11.release.approvals.check` finds every approval the design
requires (apply after the 10/13 recording).
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

try:  # module11.aws in the repo; flat in the corpus handler's Lambda zip
    from . import gitlab_api as G
except ImportError:  # pragma: no cover - the Lambda layout
    import gitlab_api as G  # type: ignore[no-redef]

BOT_BRANCH = re.compile(r"^(demo|corpus|recommendation|scenario|model)/[A-Za-z0-9._/-]{1,150}$")


@dataclass
class ChangeRequest:
    iid: int
    web_url: str
    branch: str
    reused: bool
    sha: str | None = None


def actions(files: Mapping[str, bytes], existing: Iterable[str]) -> list[dict]:
    have = set(existing)
    return [{"action": "update" if path in have else "create", "file_path": path, "encoding": "base64",
             "content": base64.b64encode(data).decode("ascii")} for path, data in sorted(files.items())]


def open_change(gl: G.GitLab, *, branch: str, files: Mapping[str, bytes], message: str, title: str,
                description: str, labels: Iterable[str] = (), target: str = "main") -> ChangeRequest:
    if not BOT_BRANCH.match(branch) or ".." in branch or branch.endswith("/"):
        raise ValueError("bot branches start with demo/, corpus/, recommendation/, scenario/ or model/")
    open_mrs = gl.merge_requests(source_branch=branch, state="opened")
    if open_mrs:
        mr = open_mrs[0]
        return ChangeRequest(int(mr["iid"]), mr.get("web_url", ""), branch, True, mr.get("sha"))
    if gl.branch(branch) is not None:
        gl.delete_branch(branch)                      # a leftover with no open MR: start over
    existing = [p for p in files if gl.file(p, target) is not None]
    commit = gl.commit_actions(branch, message, actions(files, existing), start_branch=target)
    mr = gl.create_merge_request(branch, title, description, labels, target=target)
    return ChangeRequest(int(mr["iid"]), mr.get("web_url", ""), branch, False, commit["id"])


CODEOWNERS_PATH = ".gitlab/CODEOWNERS"


def design_verdict(gl: G.GitLab, iid: int, approvers: Mapping[str, Iterable[str]], *, mr: dict | None = None,
                   target: str = "main"):
    """`approvals.check` for one MR, from its changed paths, its approvers and the target branch's CODEOWNERS."""
    from module11.release import approvals as AP     # repo layout only; the corpus Lambda never merges
    mr = mr or gl.merge_request(iid)
    text = gl.file(CODEOWNERS_PATH, target)
    if text is None:
        return AP.Verdict(False, (f"no {CODEOWNERS_PATH} on {target}",))
    state = gl.get(f"{gl.p}/merge_requests/{int(iid)}/approvals") or {}
    approved_by = [(a.get("user") or {}).get("username") for a in state.get("approved_by") or []]
    author = (mr.get("author") or {}).get("username")
    return AP.check(gl.merge_request_paths(iid), text.decode("utf-8"), approved_by, approvers, author)


def wait_and_merge(gl: G.GitLab, iid: int, approvers: Mapping[str, Iterable[str]], *, wait_s: int = 30 * 60,
                   poll: float = 20.0, say: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                   sleep: Callable[[float], None] = time.sleep) -> dict:
    """Merge as the bot once the pipeline succeeded, GitLab says approved, and the design's approvals are all there.
    The merge pins the head SHA that was checked, so a push after the check makes GitLab refuse it."""
    end = clock() + wait_s
    last: tuple[str, ...] = ()
    while clock() < end:
        mr = gl.merge_request(iid)
        pipeline = (mr.get("head_pipeline") or {}).get("status")
        if pipeline == "failed":
            raise RuntimeError(f"!{iid}'s pipeline failed; fix it before re-running")
        gitlab_ok = bool((gl.get(f"{gl.p}/merge_requests/{int(iid)}/approvals") or {}).get("approved"))
        verdict = design_verdict(gl, iid, approvers, mr=mr, target=mr.get("target_branch") or "main")
        if pipeline == "success" and gitlab_ok and verdict.ok:
            merged = gl.merge(iid, mr["sha"])
            say(f"merged !{iid} ({str(merged.get('merge_commit_sha', ''))[:8]})")
            return merged
        if verdict.missing != last:
            last = verdict.missing
            for reason in last:
                say(f"!{iid} waiting: {reason}")
        sleep(poll)
    raise RuntimeError(f"!{iid} did not get a green pipeline and the approvals the design requires within "
                       f"{wait_s // 60} minutes")
