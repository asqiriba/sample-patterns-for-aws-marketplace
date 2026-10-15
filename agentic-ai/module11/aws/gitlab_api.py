# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GitLab REST API v4 for the release tooling (the Seal stage and the MR jobs).

The bot token (`devops-companion/partners/gitlab-token`, a project access token) is read from
Secrets Manager into memory by `from_secret()` and held only by this object: it is never printed,
logged, written to disk, put in a URL or passed to a subprocess. Error messages carry the method,
the path and the HTTP status, never a header.

Standard library only (urllib), so the `m11-gitlab-notes` broker Lambda packages this file as is.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

API = "https://gitlab.com/api/v4"
SHA = re.compile(r"^[0-9a-f]{40}$")
TAG = re.compile(r"^harness/v(\d{1,6})$")
BRANCH = re.compile(r"^(?!.*\.\.)(?!/)[A-Za-z0-9._/-]{1,200}(?<!/)$")
PACKAGE = "devops-companion-harness"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GitLabError(RuntimeError):
    def __init__(self, method: str, path: str, status: int | None, detail: str = "") -> None:
        super().__init__(f"GitLab {method} {path.split('?')[0]} -> {status}{': ' + detail if detail else ''}")
        self.status = status


def _quote(value: str | int) -> str:
    return urllib.parse.quote(str(value), safe="")


def check_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.match(value):
        raise ValueError("expected a 40-character lowercase hex commit SHA")
    return value


def check_branch(value: str) -> str:
    if not isinstance(value, str) or not BRANCH.match(value):
        raise ValueError("invalid branch name")
    return value


def tag_number(tag: str) -> int | None:
    m = TAG.match(tag or "")
    return int(m.group(1)) if m else None


def next_tag(existing: Iterable[str]) -> str:
    """`harness/v<n+1>` after the highest `harness/v<n>` (v1 when none exists)."""
    numbers = [n for n in (tag_number(t) for t in existing) if n is not None]
    return f"harness/v{max(numbers, default=0) + 1}"


def approval_line(message: str | None) -> str | None:
    """`default` or `required` from an annotated tag message (`approval: <level>`)."""
    m = re.search(r"^approval: (default|required)$", message or "", re.MULTILINE)
    return m.group(1) if m else None


@dataclass
class GitLab:
    token: str = field(repr=False)
    project_id: str
    base: str = API
    opener: Callable[..., Any] = urllib.request.urlopen
    sleep: Callable[[float], None] = time.sleep
    timeout: int = 60

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None, *, raw: bytes | None = None,
                 content_type: str = "application/json", want: str = "json") -> Any:
        data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        headers = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = content_type
        for attempt in range(4):
            request = urllib.request.Request(f"{self.base}{path}", data=data, method=method, headers=headers)
            try:
                with self.opener(request, timeout=self.timeout) as response:  # noqa: S310 - fixed https host
                    payload = response.read()
                    if want == "bytes":
                        return payload
                    if want == "response":
                        return payload, dict(response.headers)
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = str(json.loads(exc.read() or b"{}").get("message", ""))[:300]
                except (ValueError, AttributeError):
                    detail = ""
                if exc.code in RETRY_STATUSES and attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                raise GitLabError(method, path, exc.code, detail) from None
            except urllib.error.URLError as exc:
                if attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                raise GitLabError(method, path, None, str(exc.reason)[:200]) from None
        raise GitLabError(method, path, None, "retries exhausted")

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self._request("POST", path, body)

    def put(self, path: str, body: Any = None) -> Any:
        return self._request("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def paginate(self, path: str, limit: int = 2000) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while len(out) < limit:
            items = self.get(f"{path}{sep}per_page=100&page={page}") or []
            out += items
            if len(items) < 100:
                return out
            page += 1
        return out

    @property
    def p(self) -> str:
        return f"/projects/{_quote(self.project_id)}"

    # ------------------------------------------------------------ identity
    def current_user(self) -> dict:
        return self.get("/user")

    def project(self) -> dict:
        return self.get(self.p)

    # ------------------------------------------------------------ commits, branches, files
    def commit(self, ref: str) -> dict:
        return self.get(f"{self.p}/repository/commits/{_quote(ref)}")

    def resolve(self, ref: str) -> str:
        """A branch, tag or SHA -> its 40-hex commit SHA (the shadow experiment's `--source`)."""
        sha = self.commit(ref)["id"]
        return check_sha(sha)

    def branch(self, name: str) -> dict | None:
        try:
            return self.get(f"{self.p}/repository/branches/{_quote(check_branch(name))}")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def create_branch(self, name: str, ref: str) -> dict:
        return self.post(f"{self.p}/repository/branches", {"branch": check_branch(name), "ref": ref})

    def delete_branch(self, name: str) -> None:
        self.delete(f"{self.p}/repository/branches/{_quote(check_branch(name))}")

    def file(self, path: str, ref: str) -> bytes | None:
        try:
            return self._request("GET", f"{self.p}/repository/files/{_quote(path)}/raw?ref={_quote(ref)}", want="bytes")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def commit_actions(self, branch: str, message: str, actions: list[dict], *, start_branch: str | None = None) -> dict:
        """Commits API (web commits; GitLab signs them when web-based commit signing is on)."""
        body = {"branch": check_branch(branch), "commit_message": message, "actions": actions}
        if start_branch:
            body["start_branch"] = check_branch(start_branch)
        return self.post(f"{self.p}/repository/commits", body)

    def commit_signature(self, sha: str) -> dict | None:
        try:
            return self.get(f"{self.p}/repository/commits/{check_sha(sha)}/signature")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def archive(self, sha: str) -> bytes:
        """`GET /repository/archive.zip?sha=` (GitLab.com allows 5 per minute)."""
        return self._request("GET", f"{self.p}/repository/archive.zip?sha={check_sha(sha)}", want="bytes")

    # ------------------------------------------------------------ tags, releases, packages (Seal)
    def tags(self, search: str = "harness/v") -> list[dict]:
        return self.paginate(f"{self.p}/repository/tags?search={_quote('^' + search)}")

    def tag(self, name: str) -> dict | None:
        try:
            return self.get(f"{self.p}/repository/tags/{_quote(name)}")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def create_tag(self, name: str, sha: str, message: str) -> dict:
        """An annotated tag (a message makes it annotated); only the bot may create `harness/v*`."""
        if tag_number(name) is None:
            raise ValueError("release tags are harness/v<n>")
        return self.post(f"{self.p}/repository/tags", {"tag_name": name, "ref": check_sha(sha), "message": message})

    def release(self, tag: str) -> dict | None:
        try:
            return self.get(f"{self.p}/releases/{_quote(tag)}")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def create_release(self, tag: str, name: str, description: str, links: list[dict]) -> dict:
        return self.post(f"{self.p}/releases", {"tag_name": tag, "name": name, "description": description,
                                                "assets": {"links": links}})

    def package_url(self, version: str | int, filename: str) -> str:
        return f"{self.base}{self.p}/packages/generic/{PACKAGE}/{_quote(version)}/{_quote(filename)}"

    def upload_package_file(self, version: str | int, filename: str, data: bytes) -> dict:
        path = f"{self.p}/packages/generic/{PACKAGE}/{_quote(version)}/{_quote(filename)}?status=default"
        return self._request("PUT", path, raw=data, content_type="application/octet-stream")

    def package_files(self, version: str | int) -> list[str]:
        packages = self.get(f"{self.p}/packages?package_type=generic&package_name={PACKAGE}&per_page=100") or []
        match = [p for p in packages if p.get("name") == PACKAGE and str(p.get("version")) == str(version)]
        if not match:
            return []
        files = self.get(f"{self.p}/packages/{match[0]['id']}/package_files?per_page=100") or []
        return sorted({f["file_name"] for f in files})

    # ------------------------------------------------------------ merge requests and notes
    def merge_requests(self, *, source_branch: str | None = None, state: str = "opened", labels: str | None = None) -> list:
        q = f"state={state}"
        if source_branch:
            q += f"&source_branch={_quote(check_branch(source_branch))}"
        if labels:
            q += f"&labels={_quote(labels)}"
        return self.paginate(f"{self.p}/merge_requests?{q}")

    def merge_request(self, iid: int) -> dict:
        return self.get(f"{self.p}/merge_requests/{int(iid)}")

    def merge_request_paths(self, iid: int) -> list[str]:
        diffs = self.paginate(f"{self.p}/merge_requests/{int(iid)}/diffs")
        return sorted({d.get("new_path") or d.get("old_path") for d in diffs} - {None})

    def create_merge_request(self, source: str, title: str, description: str, labels: Iterable[str] = (),
                             target: str = "main") -> dict:
        return self.post(f"{self.p}/merge_requests", {
            "source_branch": check_branch(source), "target_branch": check_branch(target), "title": title,
            "description": description, "labels": ",".join(labels), "remove_source_branch": True})

    def approval_state(self, iid: int) -> list[dict]:
        """The MR's approval rules as GitLab evaluates them (code-owner sections, any_approver), compacted to
        {name, type, section, required, approvedBy, approved}; read by preflight item 8 and the governance checks."""
        state = self.get(f"{self.p}/merge_requests/{int(iid)}/approval_state") or {}
        return [{"name": r.get("name"), "type": r.get("rule_type"), "section": r.get("section"),
                 "required": r.get("approvals_required"), "approved": bool(r.get("approved")),
                 "approvedBy": sorted(u.get("username") for u in r.get("approved_by") or [])}
                for r in state.get("rules") or []]

    def protected_branch(self, name: str) -> dict | None:
        try:
            return self.get(f"{self.p}/protected_branches/{_quote(name)}")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def push_rule(self) -> dict:
        return self.get(f"{self.p}/push_rule") or {}

    def merge(self, iid: int, sha: str, *, when_pipeline_succeeds: bool = False) -> dict:
        """Bot merges remove the source branch and pin the approved head."""
        body = {"should_remove_source_branch": True, "sha": check_sha(sha)}
        if when_pipeline_succeeds:
            body["merge_when_pipeline_succeeds"] = True
        return self.put(f"{self.p}/merge_requests/{int(iid)}/merge", body)

    def commit_note(self, sha: str, body: str) -> dict:
        return self.post(f"{self.p}/repository/commits/{check_sha(sha)}/comments", {"note": body})

    def upsert_mr_note(self, iid: int, body: str, marker: str, author_id: int | None = None) -> dict:
        """Update the bot's note carrying `marker`, else create one."""
        if marker not in body:
            body = f"{body}\n\n{marker}"
        for note in self.paginate(f"{self.p}/merge_requests/{int(iid)}/notes?sort=asc"):
            if marker in (note.get("body") or "") and (author_id is None or note.get("author", {}).get("id") == author_id):
                return self.put(f"{self.p}/merge_requests/{int(iid)}/notes/{int(note['id'])}", {"body": body})
        return self.post(f"{self.p}/merge_requests/{int(iid)}/notes", {"body": body})

    # ------------------------------------------------------------ jobs, deployments, environments
    def job(self, job_id: int) -> dict:
        return self.get(f"{self.p}/jobs/{int(job_id)}")

    def deployments(self, environment: str) -> list[dict]:
        return self.paginate(f"{self.p}/deployments?environment={_quote(environment)}&order_by=id&sort=desc", 300)

    def environment(self, name: str) -> dict | None:
        found = [e for e in self.get(f"{self.p}/environments?name={_quote(name)}") or [] if e.get("name") == name]
        return found[0] if found else None

    def stop_environment(self, environment_id: int) -> dict:
        return self.post(f"{self.p}/environments/{int(environment_id)}/stop")

    def group_member(self, group: str | int, user_id: int) -> dict | None:
        try:
            return self.get(f"/groups/{_quote(group)}/members/all/{int(user_id)}")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def pipelines(self, *, ref: str | None = None, sha: str | None = None) -> list[dict]:
        q = "order_by=id&sort=desc"
        if ref:
            q += f"&ref={_quote(ref)}"
        if sha:
            q += f"&sha={check_sha(sha)}"
        return self.get(f"{self.p}/pipelines?{q}&per_page=20") or []

    def pipeline_jobs(self, pipeline_id: int) -> list[dict]:
        return self.paginate(f"{self.p}/pipelines/{int(pipeline_id)}/jobs")

    def cancel_job(self, job_id: int) -> dict:
        return self.post(f"{self.p}/jobs/{int(job_id)}/cancel")

    # ------------------------------------------------------------ keys (partners up)
    def add_signing_key(self, title: str, public_key: str, expires_at: str) -> dict:
        return self.post("/user/keys", {"title": title, "key": public_key, "usage_type": "signing",
                                        "expires_at": expires_at})

    def keys(self) -> list[dict]:
        return self.get("/user/keys") or []


def from_secret(secrets_client: Any, secret_id: str, project_id: str, **kwargs: Any) -> GitLab:
    """The bot client; the token goes from Secrets Manager into this object only."""
    value = secrets_client.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    if value.startswith("{"):
        value = str(json.loads(value).get("token", "")).strip()
    if not value:
        raise RuntimeError(f"secret {secret_id} holds no token")
    return GitLab(token=value, project_id=str(project_id), **kwargs)
