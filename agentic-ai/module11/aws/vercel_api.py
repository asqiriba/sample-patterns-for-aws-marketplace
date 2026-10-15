# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Vercel REST API for the per-MR UI preview.

The team token (`devops-companion/partners/vercel-token`) is read from Secrets Manager into memory by
`from_secret()` and held only by this object: never printed, logged, written or put in a URL. Errors carry the
method, the path and the HTTP status. Standard library only (urllib), like gitlab_api.py.

Measured live (2026-10-09):
- A branch's alias is `<project>-git-<branch slug>-<team slug>.vercel.app`, the slug being the branch name
  lower-cased with every other character than [a-z0-9] turned into `-` (`m11-test/ui-preview-check` ->
  `...-git-m11-test-ui-preview-check-<team-slug>.vercel.app`). Longer labels are not predicted: the alias is read back
  from the deployment and the stack re-applied when it differs (the design's fallback).
- Neither a git-triggered nor an API-triggered deployment of a bot-authored commit was blocked by Vercel;
  both cloned and ran the Ignored Build Step, which canceled them (`ignore.mjs`). (b) Vercel then posts a
  `success` commit status ("Canceled by Ignored Build Step"), so nothing turns a GitLab pipeline red.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

API = "https://api.vercel.com"
TERMINAL = {"READY", "ERROR", "CANCELED", "BLOCKED"}
RETRY_STATUSES = {429, 500, 502, 503, 504}


class VercelError(RuntimeError):
    def __init__(self, method: str, path: str, status: int | None, detail: str = "") -> None:
        super().__init__(f"Vercel {method} {path.split('?')[0]} -> {status}{': ' + detail if detail else ''}")
        self.status = status


def branch_slug(branch: str) -> str:
    return re.sub(r"[^a-z0-9]", "-", branch.lower())


def predict_alias(project: str, branch: str, scope: str) -> str:
    """The observed rule; the deploy reads the real alias back and re-applies if it differs."""
    return f"{project}-git-{branch_slug(branch)}-{scope}.vercel.app"


def branch_alias(aliases: list[str], project: str, scope: str) -> str | None:
    """The deployment's branch alias (`<project>-git-…-<scope>.vercel.app`), not its per-deployment URL."""
    prefix, suffix = f"{project}-git-", f"-{scope}.vercel.app"
    found = [a for a in aliases if a.startswith(prefix) and a.endswith(suffix)]
    return found[0] if found else None


@dataclass
class Vercel:
    token: str = field(repr=False)
    team_id: str
    base: str = API
    opener: Callable[..., Any] = urllib.request.urlopen
    sleep: Callable[[float], None] = time.sleep
    timeout: int = 60

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        sep = "&" if "?" in path else "?"
        url = f"{self.base}{path}{sep}teamId={urllib.parse.quote(self.team_id)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self.opener(request, timeout=self.timeout) as response:  # noqa: S310 - fixed https host
                    payload = response.read()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = str((json.loads(exc.read() or b"{}").get("error") or {}).get("message", ""))[:300]
                except (ValueError, AttributeError):
                    detail = ""
                if exc.code in RETRY_STATUSES and attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                raise VercelError(method, path, exc.code, detail) from None
            except urllib.error.URLError as exc:
                if attempt < 3:
                    self.sleep(2 ** attempt)
                    continue
                raise VercelError(method, path, None, str(exc.reason)[:200]) from None
        raise VercelError(method, path, None, "retries exhausted")

    def members(self) -> list[dict]:
        """GET /v3/teams/{teamId}/members: each member's email and `gitlab.login`."""
        out = self._request("GET", f"/v3/teams/{urllib.parse.quote(self.team_id)}/members?limit=100") or {}
        return list(out.get("members") or [])

    def project(self, project_id: str) -> dict:
        return self._request("GET", f"/v9/projects/{urllib.parse.quote(project_id)}") or {}

    def create_git_deployment(self, *, project_id: str, name: str, gitlab_project_id: str, ref: str, sha: str) -> dict:
        """One API-triggered git deployment of the branch head; later pushes build through the
        git integration."""
        return self._request("POST", "/v13/deployments", {     # no `target`: a branch deployment is a preview
            "name": name, "project": project_id,
            "gitSource": {"type": "gitlab", "projectId": str(gitlab_project_id), "ref": ref, "sha": sha}}) or {}

    def deployment(self, deployment_id: str) -> dict:
        return self._request("GET", f"/v13/deployments/{urllib.parse.quote(deployment_id)}") or {}

    def wait(self, deployment_id: str, *, timeout_s: float = 600, poll_s: float = 10) -> dict:
        """Poll until READY, ERROR, CANCELED or BLOCKED (or the timeout: the last state is returned)."""
        deadline = time.time() + timeout_s
        while True:
            d = self.deployment(deployment_id)
            if d.get("readyState") in TERMINAL or time.time() >= deadline:
                return d
            self.sleep(poll_s)

    def aliases(self, deployment_id: str) -> list[str]:
        out = self._request("GET", f"/v2/deployments/{urllib.parse.quote(deployment_id)}/aliases") or {}
        return [a.get("alias") for a in out.get("aliases") or [] if a.get("alias")]


def from_secret(secrets_client: Any, secret_id: str, team_id: str, **kwargs: Any) -> Vercel:
    value = secrets_client.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    if value.startswith("{"):
        value = str(json.loads(value).get("token", "")).strip()
    if not value:
        raise RuntimeError(f"secret {secret_id} holds no token")
    return Vercel(token=value, team_id=team_id, **kwargs)
