# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Vercel UI preview of an `mr-*` stack, driven by `release deploy` in the trusted
m11-preview build.

Until the stack records `uiPreview: wired`, every mr-* deploy decides (`release/ui_preview.decide`):
- skip: the stack deploys without its Vercel variables (the Cognito client still registers the predicted alias)
  and the m11-gate note says `UI preview: skipped (<reason>)`; `preview:deploy` stays green;
- wire: `enableVercel` (with the project, team and branch) goes into the stack config, so the `pulumi up` writes the
  three branch-scoped preview variables; then one API-triggered git deployment of the head commit, READY, the
  branch alias read back, a re-apply when it differs from `uiOrigin`, and the smoke checks: `/config.json`
  through the protection bypass names this stack's API, and the hosted authorize page for this stack's browser
  client and the alias returns the login form. Only then is `uiPreview: wired` recorded. A deployment that ends
  BLOCKED is `skip("blocked")`, not a failure.
Later pushes build through Vercel's git integration; the alias does not change, so nothing is rewired.
The Vercel token and the bypass secret stay in memory: never printed, logged or written.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from module11.release import ui_preview as UI

from . import vercel_api as V
from .urls import open_url

PROJECT_NAME = "devops-companion-ui"
TOKEN_SECRET = "devops-companion/partners/vercel-token"  # nosec B105 - a Secrets Manager secret name, not a secret value
BYPASS_SECRET = "devops-companion/partners/vercel-bypass"  # nosec B105 - a Secrets Manager secret name, not a secret value


@dataclass(frozen=True)
class MrHead:
    """What the preview build knows about the MR (through the notes broker)."""
    iid: int
    branch: str
    sha: str
    author_username: str
    commit_author_email: str


@dataclass
class UiWiring:
    head: MrHead
    shared: dict                                  # /m11/devops-companion/shared/outputs
    vercel: V.Vercel | None                       # None: the token could not be read (skip "unavailable")
    team_slug: str
    gitlab_project_id: str = ""
    bypass: Callable[[], str] = field(repr=False, default=lambda: "")
    fetch: Callable[..., tuple[int, str]] | None = None
    log: Callable[[str], None] = print
    decision: UI.UiDecision | None = None

    @property
    def project_id(self) -> str:
        return str(self.shared.get("vercelProjectId") or "")

    def predicted_origin(self) -> str:
        return "https://" + V.predict_alias(PROJECT_NAME, self.head.branch, self.team_slug)

    def decide(self, already_wired: bool) -> UI.UiDecision:
        bot = UI.Bot(login=str(self.shared.get("botLogin") or ""), commit_email=str(self.shared.get("botCommitEmail") or ""))
        members = None
        if self.vercel is not None and self.project_id:
            try:
                members = self.vercel.members()
            except V.VercelError as exc:
                self.log(f"Vercel members unavailable ({exc.status})")
        self.decision = UI.decide({"author_email": self.head.commit_author_email}, self.head.author_username,
                                  members, bot, already_wired=already_wired)
        self.log(f"UI decision: {self.decision.note}")
        return self.decision

    def config(self) -> dict[str, str]:
        """The stack config a `wire` decision sets before `pulumi up`."""
        return {"enableVercel": "true", "vercelProjectId": self.project_id,
                "vercelTeamId": self.vercel.team_id if self.vercel else "", "gitBranch": self.head.branch}

    def env(self) -> dict[str, str]:
        return {"VERCEL_API_TOKEN": self.vercel.token} if self.vercel else {}

    # ------------------------------------------------------------ after the stack is up
    def wire(self, outputs: dict, *, reapply: Callable[[str], dict]) -> dict:
        """Deployment, alias read-back (re-apply on a difference) and smoke. -> the ui record for the MR note."""
        t0 = time.time()
        d = self.vercel.create_git_deployment(project_id=self.project_id, name=PROJECT_NAME,
                                              gitlab_project_id=self.gitlab_project_id,
                                              ref=self.head.branch, sha=self.head.sha)
        deployment_id = d["id"]
        self.log(f"Vercel deployment {deployment_id} for {self.head.sha[:12]} started")
        d = self.vercel.wait(deployment_id)
        state = d.get("readyState")
        if state == "BLOCKED":
            self.decision = UI.blocked()
            return self.record("skip", state=state, deployment=deployment_id, seconds=time.time() - t0)
        if state != "READY":
            raise RuntimeError(f"Vercel deployment {deployment_id} ended {state}")
        alias = V.branch_alias(list(d.get("alias") or []) or self.vercel.aliases(deployment_id), PROJECT_NAME,
                               self.team_slug)
        if not alias:
            raise RuntimeError(f"Vercel deployment {deployment_id} has no branch alias")
        origin = "https://" + alias
        realigned = origin != outputs.get("uiOrigin")
        if realigned:          # fallback: the alias differs from the prediction; re-register it
            self.log(f"branch alias {alias} differs from uiOrigin {outputs.get('uiOrigin')}: re-applying")
            outputs = reapply(origin)
        self.smoke(alias, outputs)
        return self.record("wire", alias=alias, origin=origin, state=state, deployment=deployment_id,
                           realigned=realigned, seconds=time.time() - t0)

    def smoke(self, alias: str, outputs: dict) -> None:
        fetch = self.fetch or _fetch
        status, body = fetch(f"https://{alias}/config.json", {"x-vercel-protection-bypass": self.bypass()})
        if status != 200:
            raise RuntimeError(f"UI smoke: /config.json returned {status}")
        doc = json.loads(body)
        if str(doc.get("apiUrl", "")).rstrip("/") != str(outputs.get("apiUrl", "")).rstrip("/"):
            raise RuntimeError("UI smoke: /config.json names another API")
        if doc.get("clientId") != outputs.get("browserClientId") or doc.get("redirectUri") != f"https://{alias}/":
            raise RuntimeError("UI smoke: /config.json has another client or redirect URI")
        query = urllib.parse.urlencode({"client_id": outputs["browserClientId"], "response_type": "code",
                                        "scope": "openid module10/invoke", "redirect_uri": f"https://{alias}/"})
        status, page = fetch(f"{str(outputs.get('browserAuthUrl', '')).rstrip('/')}/oauth2/authorize?{query}", {})
        if status != 200 or "<form" not in page.lower():
            raise RuntimeError(f"UI smoke: the hosted authorize page returned {status} for the alias")
        self.log(f"UI smoke: /config.json names this stack; hosted sign-in 200 for https://{alias}/")

    def record(self, action: str, **fields: Any) -> dict:
        out = {"action": action, "note": (self.decision or UI.UiDecision(action)).note, "iid": self.head.iid,
               "branch": self.head.branch, "sha": self.head.sha}
        out.update({k: (round(v, 1) if isinstance(v, float) else v) for k, v in fields.items()})
        return out


def token_env(session: Any) -> dict[str, str]:
    """VERCEL_API_TOKEN for a `pulumi` subprocess that changes or deletes the stack's Vercel variables."""
    value = session.client("secretsmanager").get_secret_value(SecretId=TOKEN_SECRET)["SecretString"].strip()
    if value.startswith("{"):
        value = str(json.loads(value).get("token", "")).strip()
    return {"VERCEL_API_TOKEN": value}


def for_preview(session: Any, head: MrHead, shared: dict, *, gitlab_project_id: str, log=print) -> UiWiring:
    """The preview build's wiring: the team token and bypass secret from Secrets Manager (m11-preview's grant)."""
    sm = session.client("secretsmanager")
    team = str(shared.get("vercelTeamId") or "")
    vercel = None
    if shared.get("vercelProjectId") and team:
        try:
            vercel = V.from_secret(sm, TOKEN_SECRET, team)
        except Exception as exc:  # noqa: BLE001 - no token: the decision is skip("unavailable")
            log(f"Vercel token unavailable ({type(exc).__name__})")
    return UiWiring(head=head, shared=shared, vercel=vercel, team_slug=str(shared.get("vercelTeamSlug") or ""),
                    gitlab_project_id=gitlab_project_id, log=log,
                    bypass=lambda: sm.get_secret_value(SecretId=BYPASS_SECRET)["SecretString"].strip())


def _fetch(url: str, headers: dict) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={k: v for k, v in headers.items() if v})
    try:
        with open_url(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def note_line(ui: dict | None) -> str:
    """The m11-gate note's UI line (the gate action appends it)."""
    if not ui:
        return "UI preview: not recorded for this commit"
    if ui.get("action") == "wire":
        return f"UI preview: wired, sign in on the branch alias {ui.get('origin', '')}/"
    return ui.get("note") or "UI preview: skipped"
