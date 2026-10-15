# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The m11-gitlab-notes broker: fixed, validated operations; everything else is refused and no
rejected value is echoed. No network: a fake GitLab client records the calls."""
from __future__ import annotations

import hashlib

import pytest

from module11.aws import gitlab_api as G
from module11.lambdas.gitlab_notes import handler as H

SHA = "a" * 40


class FakeGitLab:
    def __init__(self) -> None:
        self.calls = []

    def upsert_mr_note(self, iid, body, marker, author_id=None):
        self.calls.append(("mr_note", iid, marker in body or marker))
        return {"id": 5}

    def commit_note(self, sha, body):
        self.calls.append(("commit_note", sha))
        return {"note": body}

    def merge_request(self, iid):
        return {"iid": iid, "state": "opened", "target_branch": "main", "source_branch": "feature",
                "source_project_id": 1, "target_project_id": 1, "sha": SHA, "web_url": "https://x", "author": {"id": 9}}

    def merge_request_paths(self, iid):
        return ["module11/harness/agent/prompts/companion.md"]

    def commit(self, ref):
        return {"id": SHA, "created_at": "t", "title": "x"}

    def job(self, job_id):
        return {"id": job_id, "name": "preview:deploy-infra", "status": "running",
                "pipeline": {"id": 3, "sha": SHA, "ref": "refs/merge-requests/4/head", "source": "merge_request_event"}}

    def deployments(self, environment):
        return [{"id": 77, "deployable": {"id": 12}, "status": "blocked",
                 "approvals": [{"user": {"id": 1, "username": "p"}, "status": "approved"}]}]

    def group_member(self, group, user_id):
        return {"access_level": 30} if user_id == 1 else None

    def environment(self, name):
        return {"id": 2, "state": "stopped"} if name == "review/mr-4" else None

    def archive(self, sha):
        self.calls.append(("archive", sha))
        return b"zip-bytes"


class FakeS3:
    def __init__(self) -> None:
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


def test_each_operation_returns_only_its_fields():
    gl = FakeGitLab()
    assert H.dispatch({"operation": "mr_note", "iid": 4, "body": "gate PASS"}, gl) == {"noteId": 5}
    assert H.dispatch({"operation": "commit_note", "sha": SHA, "body": "PROMOTED"}, gl)["created"]
    mr = H.dispatch({"operation": "merge_request", "iid": "4"}, gl)
    assert mr["targetBranch"] == "main" and mr["paths"] and "token" not in str(mr)
    assert H.dispatch({"operation": "commit", "ref": "experiment/model-x"}, gl)["id"] == SHA
    assert H.dispatch({"operation": "job", "job_id": 12}, gl)["pipeline"]["sha"] == SHA
    dep = H.dispatch({"operation": "deployment", "environment": "review-infra", "job_id": 12}, gl)
    assert dep["found"] and dep["approvals"] == [{"userId": 1, "username": "p", "status": "approved"}]
    assert H.dispatch({"operation": "deployment", "environment": "review-infra", "job_id": 13}, gl) == {"found": False}
    assert H.dispatch({"operation": "group_member", "group": "example-group/platform", "user_id": 1}, gl,
                      group_root="example-group")["member"]
    assert H.dispatch({"operation": "environment", "name": "review/mr-4"}, gl)["state"] == "stopped"


def test_group_member_is_pinned_to_the_configured_group(monkeypatch):
    """Only direct subgroups of M11_GITLAB_GROUP (or numeric IDs) may be queried; no group configured means IDs only."""
    gl = FakeGitLab()
    monkeypatch.setenv("M11_GITLAB_GROUP", "example-group")
    assert H.dispatch({"operation": "group_member", "group": "example-group/quality", "user_id": 1}, gl)["member"]
    for group in ("other-group/platform", "example-group/platform/x", "example-group", "../platform"):
        with pytest.raises(H.Rejected):
            H.dispatch({"operation": "group_member", "group": group, "user_id": 1}, gl)
    monkeypatch.delenv("M11_GITLAB_GROUP")
    with pytest.raises(H.Rejected):
        H.dispatch({"operation": "group_member", "group": "example-group/platform", "user_id": 1}, gl)
    assert H.dispatch({"operation": "group_member", "group": "4242", "user_id": 1}, gl)["member"]


def test_archive_goes_to_s3_with_its_sha256():
    gl, s3 = FakeGitLab(), FakeS3()
    out = H.dispatch({"operation": "archive", "sha": SHA}, gl, s3=s3, bucket="m11-release-x")
    assert out == {"key": f"previews/archives/{SHA}.zip", "sha256": hashlib.sha256(b"zip-bytes").hexdigest(), "bytes": 9}
    assert s3.puts[0]["Key"] == out["key"] and s3.puts[0]["Metadata"]["m11-sha256"] == out["sha256"]
    with pytest.raises(H.Rejected):
        H.dispatch({"operation": "archive", "sha": SHA}, gl)          # no bucket configured


@pytest.mark.parametrize("event", [
    {"operation": "delete_project"}, {"operation": "raw", "path": "/projects"}, {}, {"operation": None},
    {"operation": "commit_note", "sha": "abc", "body": "x"},
    {"operation": "commit_note", "sha": SHA.upper(), "body": "x"},
    {"operation": "commit_note", "sha": SHA, "body": ""},
    {"operation": "commit_note", "sha": SHA, "body": "x" * (H.MAX_BODY + 1)},
    {"operation": "mr_note", "iid": "1; DROP", "body": "x"},
    {"operation": "mr_note", "iid": True, "body": "x"},
    {"operation": "job", "job_id": "12/../../tokens"},
    {"operation": "deployment", "environment": "staging", "job_id": 1},
    {"operation": "environment", "name": "review/../../production"},
    {"operation": "group_member", "group": "other-group/x", "user_id": 1},
    {"operation": "commit", "ref": "main..HEAD"},
])
def test_unknown_operations_and_bad_arguments_are_refused(event):
    gl = FakeGitLab()
    with pytest.raises(H.Rejected) as exc:
        H.dispatch(event, gl)
    assert "12/../../tokens" not in str(exc.value) and "DROP" not in str(exc.value)
    assert gl.calls == []


def test_handler_maps_rejections_and_api_errors_without_secrets(monkeypatch):
    monkeypatch.setattr(H, "_gitlab", lambda secrets: FakeGitLab())
    out = H.handler({"operation": "nope"}, make=lambda name: None)
    assert out == {"ok": False, "error": "Rejected", "message": "unknown operation"}

    class Failing(FakeGitLab):
        def commit(self, ref):
            raise G.GitLabError("GET", "/projects/1/repository/commits/x?private_token=no", 404, "not found")
    monkeypatch.setattr(H, "_gitlab", lambda secrets: Failing())
    out = H.handler({"operation": "commit", "ref": "main"}, make=lambda name: None)
    assert out["ok"] is False and out["status"] == 404 and "private_token" not in out["message"]


def test_token_comes_from_the_secret_and_never_reaches_repr():
    class Secrets:
        def get_secret_value(self, SecretId):
            return {"SecretString": "glpat-secret-value\n"}
    gl = G.from_secret(Secrets(), "devops-companion/partners/gitlab-token", "12345678")
    assert gl.token == "glpat-secret-value" and "glpat" not in repr(gl)  # nosec B105 - dummy token from the test's fake Secrets Manager
