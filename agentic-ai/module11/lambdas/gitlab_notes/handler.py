# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-gitlab-notes: the GitLab broker for builds that must never hold a GitLab token.

The GitLab Ultimate trial allows one project access token, the bot's. Design H3 requires that no
preview project can read it, so m11-preview, m11-preview-infra, m11-gate, m11-bake and
m11-seal-experiment invoke this function instead of reading the secret. It performs a fixed set of
validated operations and refuses everything else; it never returns the token or echoes a rejected
value.

Operations (event `{"operation": <name>, ...}`):
    mr_note        {iid, body}            create or update the marked `<!-- m11-gate -->` MR note
    commit_note    {sha, body}            post a note on a commit (Bake's rollout outcome)
    merge_request  {iid}                  state, branches, projects, head SHA and changed paths
    commit         {ref}                  {id, created_at, title} (a branch, tag or SHA)
    job            {job_id}               name, status, pipeline id/sha/ref/source
    deployment     {environment, job_id}  the job's deployment in that environment with its approvals
    group_member   {group, user_id}       whether the user is a (direct or inherited) member
    environment    {name}                 the environment's state (review/mr-<iid>, review-infra)
    archive        {sha, key_prefix}      export the commit archive to s3://<bucket>/previews/archives/<sha>.zip

Configuration (environment): M11_GITLAB_PROJECT_ID, NOTES_CREDENTIAL_SECRET (an ARN; default secret
devops-companion/partners/gitlab-token), RELEASE_BUCKET, M11_GITLAB_GROUP (the top-level group whose direct
subgroups `group_member` may query).
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Callable

try:  # packaged as a top-level module in the Lambda zip; module11.aws.gitlab_api in tests
    from module11.aws import gitlab_api as G
except ImportError:  # pragma: no cover - Lambda layout
    import gitlab_api as G  # type: ignore[no-redef]

MARKER = "<!-- m11-gate -->"
MAX_BODY = 60_000
IID = re.compile(r"^\d{1,9}$")
JOB = re.compile(r"^\d{1,12}$")
USER = re.compile(r"^\d{1,12}$")
GROUP_ID = re.compile(r"^\d{1,12}$")
GROUP_ROOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}){0,3}$")
ENVIRONMENT = re.compile(r"^(review/mr-\d{1,9}|review-infra|production|production-gated)$")
REF = re.compile(r"^(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")
ARCHIVE_PREFIX = "previews/archives/"


class Rejected(ValueError):
    """An operation or argument outside the broker's fixed set (the value is never echoed)."""


def _field(event: dict, name: str, pattern: re.Pattern) -> str:
    value = event.get(name)
    value = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not pattern.match(value):
        raise Rejected(f"invalid {name}")
    return value


def _body(event: dict) -> str:
    body = event.get("body")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        raise Rejected("invalid body")
    return body


def group_pattern(root: str) -> re.Pattern:
    """`group_member` accepts a numeric group ID or a direct subgroup of the configured group (M11_GITLAB_GROUP,
    for example `<your-group>/platform`). With no valid group configured, only numeric IDs are accepted."""
    if not GROUP_ROOT.match(root or ""):
        return GROUP_ID
    return re.compile(rf"^{re.escape(root)}/[a-z0-9-]{{1,60}}$|^\d{{1,12}}$")


def _sha(event: dict) -> str:
    value = event.get("sha")
    if not isinstance(value, str) or not G.SHA.match(value):
        raise Rejected("invalid sha")
    return value


def _mr(gl: G.GitLab, event: dict) -> dict:
    iid = int(_field(event, "iid", IID))
    mr = gl.merge_request(iid)
    return {"iid": mr["iid"], "state": mr["state"], "targetBranch": mr["target_branch"],
            "sourceBranch": mr["source_branch"], "sourceProjectId": mr["source_project_id"],
            "targetProjectId": mr["target_project_id"], "sha": mr.get("sha"), "webUrl": mr.get("web_url"),
            "authorId": (mr.get("author") or {}).get("id"), "authorUsername": (mr.get("author") or {}).get("username"),
            "paths": gl.merge_request_paths(iid)}


def _deployment(gl: G.GitLab, event: dict) -> dict:
    environment = _field(event, "environment", ENVIRONMENT)
    job_id = int(_field(event, "job_id", JOB))
    for d in gl.deployments(environment):
        if (d.get("deployable") or {}).get("id") == job_id:
            approvals = [{"userId": (a.get("user") or {}).get("id"), "username": (a.get("user") or {}).get("username"),
                          "status": a.get("status")} for a in d.get("approvals") or []]
            return {"found": True, "id": d["id"], "status": d.get("status"), "approvals": approvals,
                    "approvalSummary": d.get("approval_summary")}
    return {"found": False}


def _archive(gl: G.GitLab, event: dict, s3: Any, bucket: str) -> dict:
    sha = _sha(event)
    key = f"{ARCHIVE_PREFIX}{sha}.zip"
    data = gl.archive(sha)
    digest = hashlib.sha256(data).hexdigest()
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/zip", Metadata={"m11-sha256": digest})
    return {"key": key, "sha256": digest, "bytes": len(data)}


OPERATIONS = ("mr_note", "commit_note", "merge_request", "commit", "job", "deployment", "group_member",
              "environment", "archive")


def dispatch(event: dict, gl: G.GitLab, *, s3: Any = None, bucket: str = "", group_root: str | None = None) -> dict:
    if not isinstance(event, dict):
        raise Rejected("invalid event")
    op = event.get("operation")
    if op not in OPERATIONS:
        raise Rejected("unknown operation")
    if op == "mr_note":
        iid = int(_field(event, "iid", IID))
        note = gl.upsert_mr_note(iid, _body(event), MARKER, author_id=None)
        return {"noteId": note.get("id")}
    if op == "commit_note":
        note = gl.commit_note(_sha(event), _body(event))
        return {"created": True, "noteId": note.get("id") if isinstance(note, dict) else None}
    if op == "merge_request":
        return _mr(gl, event)
    if op == "commit":
        ref = _field(event, "ref", REF)
        c = gl.commit(ref)
        # The author email feeds the UI-preview decision (release/ui_preview.decide).
        return {"id": c["id"], "createdAt": c.get("created_at"), "title": c.get("title"),
                "authorEmail": c.get("author_email")}
    if op == "job":
        job = gl.job(int(_field(event, "job_id", JOB)))
        pipe = job.get("pipeline") or {}
        return {"id": job["id"], "name": job.get("name"), "status": job.get("status"),
                "pipeline": {"id": pipe.get("id"), "sha": pipe.get("sha"), "ref": pipe.get("ref"),
                             "source": pipe.get("source")}}
    if op == "deployment":
        return _deployment(gl, event)
    if op == "group_member":
        root = os.environ.get("M11_GITLAB_GROUP", "") if group_root is None else group_root
        member = gl.group_member(_field(event, "group", group_pattern(root)), int(_field(event, "user_id", USER)))
        return {"member": bool(member), "accessLevel": (member or {}).get("access_level")}
    if op == "environment":
        env = gl.environment(_field(event, "name", ENVIRONMENT))
        return {"found": env is not None, "id": (env or {}).get("id"), "state": (env or {}).get("state")}
    if s3 is None or not bucket:
        raise Rejected("archive export is not configured")
    return _archive(gl, event, s3, bucket)


_CLIENT: G.GitLab | None = None


def _gitlab(secrets: Any) -> G.GitLab:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = G.from_secret(secrets, os.environ["NOTES_CREDENTIAL_SECRET"], os.environ["M11_GITLAB_PROJECT_ID"])
    return _CLIENT


def handler(event: Any, context: Any = None, *, make: Callable[[str], Any] | None = None) -> dict:
    import boto3  # Lambda runtime

    make = make or boto3.client
    try:
        gl = _gitlab(make("secretsmanager"))
        result = dispatch(event, gl, s3=make("s3"), bucket=os.environ.get("RELEASE_BUCKET", ""))
        return {"ok": True, "result": result}
    except Rejected as exc:
        return {"ok": False, "error": "Rejected", "message": str(exc)}
    except G.GitLabError as exc:
        return {"ok": False, "error": "GitLabError", "status": exc.status, "message": str(exc)}
