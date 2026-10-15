# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The mr-* UI preview: Vercel API calls over a fake transport, the alias rule, the
wire flow (deployment, READY, alias read-back with a re-apply on a difference, the /config.json and hosted sign-in
smoke), skip and blocked records, the m11-gate note line, the stack's Vercel variables under Pulumi mocks, and the
broker fields the decision reads."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from module11.aws import ui_wiring as W
from module11.aws import vercel_api as V
from module11.release import ui_preview as UI

TEAM, PROJECT = "team_EXAMPLE", "prj_EXAMPLE"
SHARED = {"vercelProjectId": PROJECT, "vercelTeamId": TEAM, "vercelTeamSlug": "example-team",
          "botLogin": "project_1_bot_x", "botCommitEmail": "project_1_bot_x@noreply.gitlab.com"}
MEMBER = {"email": "presenter@example.com", "gitlab": {"login": "presenter"}}


class FakeHTTP:
    """urlopen stand-in: routes by (method, path prefix); records requests (never their Authorization value)."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, request, timeout=60):
        path = request.full_url.split("api.vercel.com", 1)[1]
        assert "teamId=team_EXAMPLE" in path and request.headers["Authorization"].startswith("Bearer ")
        self.calls.append((request.get_method(), path.split("?")[0],
                           json.loads(request.data) if request.data else None))
        for (method, prefix), reply in self.routes.items():
            if request.get_method() == method and path.startswith(prefix):
                value = reply.pop(0) if isinstance(reply, list) else reply
                if isinstance(value, int):
                    raise urllib.error.HTTPError(request.full_url, value, "x", {}, io.BytesIO(b'{"error":{}}'))
                return io.BytesIO(json.dumps(value).encode())
        raise AssertionError(f"unrouted {request.get_method()} {path}")


def client(routes):
    http = FakeHTTP(routes)
    return V.Vercel(token="t" * 24, team_id=TEAM, opener=http, sleep=lambda _: None), http


def test_alias_rule_and_branch_alias_selection():
    assert V.predict_alias("devops-companion-ui", "m11-test/ui-preview-check", "example-team") == \
        "devops-companion-ui-git-m11-test-ui-preview-check-example-team.vercel.app"            # measured
    assert V.predict_alias("p", "Friday_Brevity", "t") == "p-git-friday-brevity-t.vercel.app"
    aliases = ["p-abc123-t.vercel.app", "p-git-friday-brevity-t.vercel.app"]
    assert V.branch_alias(aliases, "p", "t") == "p-git-friday-brevity-t.vercel.app"
    assert V.branch_alias(["p-abc123-t.vercel.app"], "p", "t") is None


def test_the_api_deployment_names_the_gitlab_ref_and_sha_and_has_no_target():
    vc, http = client({("POST", "/v13/deployments"): {"id": "dpl_1", "readyState": "INITIALIZING"}})
    vc.create_git_deployment(project_id=PROJECT, name="devops-companion-ui", gitlab_project_id="42",
                             ref="friday-brevity", sha="a" * 40)
    body = http.calls[0][2]
    assert body["gitSource"] == {"type": "gitlab", "projectId": "42", "ref": "friday-brevity", "sha": "a" * 40}
    assert "target" not in body and body["project"] == PROJECT


def test_errors_carry_the_status_not_the_token():
    vc, _ = client({("GET", "/v3/teams/"): 403})
    with pytest.raises(V.VercelError) as err:
        vc.members()
    assert err.value.status == 403 and "t" * 24 not in str(err.value)


def head(email="presenter@example.com", user="presenter", branch="friday-brevity"):
    return W.MrHead(iid=7, branch=branch, sha="b" * 40, author_username=user, commit_author_email=email)


def wiring(routes, *, h=None, fetch=None):
    vc, http = client(routes)
    return W.UiWiring(head=h or head(), shared=SHARED, vercel=vc, team_slug="example-team", gitlab_project_id="42",
                      bypass=lambda: "bypass-secret", fetch=fetch, log=lambda _: None), http


def test_decisions_member_bot_and_unavailable():
    w, _ = wiring({("GET", "/v3/teams/"): {"members": [MEMBER]}})
    assert w.decide(already_wired=False).action == UI.WIRE
    assert w.config() == {"enableVercel": "true", "vercelProjectId": PROJECT, "vercelTeamId": TEAM,
                          "gitBranch": "friday-brevity"}
    assert w.env() == {"VERCEL_API_TOKEN": "t" * 24}
    bot, _ = wiring({("GET", "/v3/teams/"): {"members": [MEMBER]}}, h=head(email=SHARED["botCommitEmail"], user="x"))
    assert bot.decide(False).note == "UI preview: skipped (commit author is the release bot)"
    down, _ = wiring({("GET", "/v3/teams/"): 503})
    assert down.decide(False).note == "UI preview: skipped (Vercel unavailable)"
    assert w.predicted_origin() == "https://devops-companion-ui-git-friday-brevity-example-team.vercel.app"


OUTPUTS = {"apiUrl": "https://api.example/api", "browserClientId": "client7", "browserAuthUrl": "https://auth.example",
           "uiOrigin": "https://devops-companion-ui-git-friday-brevity-example-team.vercel.app"}


def smoke_fetch(alias, seen):
    def fetch(url, headers):
        seen.append((url, dict(headers)))
        if url.endswith("/config.json"):
            return 200, json.dumps({"apiUrl": OUTPUTS["apiUrl"], "clientId": "client7", "redirectUri": f"https://{alias}/"})
        assert f"redirect_uri=https%3A%2F%2F{alias}%2F" in url and "client_id=client7" in url
        return 200, "<html><form action='/login'></form></html>"
    return fetch


def test_wire_reads_the_alias_back_and_smokes_config_and_sign_in():
    alias = "devops-companion-ui-git-friday-brevity-example-team.vercel.app"
    seen = []
    w, http = wiring({("POST", "/v13/deployments"): {"id": "dpl_1", "readyState": "QUEUED"},
                      ("GET", "/v13/deployments/dpl_1"): [{"readyState": "BUILDING"},
                                                         {"readyState": "READY", "alias": [alias]}],
                      ("GET", "/v3/teams/"): {"members": [MEMBER]}}, fetch=smoke_fetch(alias, seen))
    w.decide(False)
    out = w.wire(dict(OUTPUTS), reapply=lambda origin: pytest.fail("no re-apply when the prediction holds"))
    assert out["action"] == "wire" and out["origin"] == f"https://{alias}" and out["realigned"] is False
    assert seen[0] == (f"https://{alias}/config.json", {"x-vercel-protection-bypass": "bypass-secret"})
    assert W.note_line(out) == f"UI preview: wired, sign in on the branch alias https://{alias}/"


def test_a_different_alias_is_re_applied_before_the_smoke():
    alias = "devops-companion-ui-git-friday-brev-4f2a-example-team.vercel.app"     # a truncated label
    applied = []
    w, _ = wiring({("POST", "/v13/deployments"): {"id": "dpl_2"},
                   ("GET", "/v13/deployments/dpl_2"): {"readyState": "READY", "alias": []},
                   ("GET", "/v2/deployments/dpl_2/aliases"): {"aliases": [{"alias": alias}]},
                   ("GET", "/v3/teams/"): {"members": [MEMBER]}}, fetch=smoke_fetch(alias, []))
    w.decide(False)
    out = w.wire(dict(OUTPUTS), reapply=lambda origin: applied.append(origin) or {**OUTPUTS, "uiOrigin": origin})
    assert applied == [f"https://{alias}"] and out["realigned"] is True


def test_blocked_is_a_skip_and_a_failed_build_or_wrong_config_is_an_error():
    w, _ = wiring({("POST", "/v13/deployments"): {"id": "dpl_3"},
                   ("GET", "/v13/deployments/dpl_3"): {"readyState": "BLOCKED"},
                   ("GET", "/v3/teams/"): {"members": [MEMBER]}})
    w.decide(False)
    out = w.wire(dict(OUTPUTS), reapply=lambda o: OUTPUTS)
    assert out["action"] == "skip" and W.note_line(out) == "UI preview: skipped (Vercel blocked the commit author)"
    err, _ = wiring({("POST", "/v13/deployments"): {"id": "dpl_4"},
                     ("GET", "/v13/deployments/dpl_4"): {"readyState": "ERROR"}})
    with pytest.raises(RuntimeError, match="ended ERROR"):
        err.wire(dict(OUTPUTS), reapply=lambda o: OUTPUTS)
    alias = "devops-companion-ui-git-friday-brevity-example-team.vercel.app"
    bad, _ = wiring({("POST", "/v13/deployments"): {"id": "dpl_5"},
                     ("GET", "/v13/deployments/dpl_5"): {"readyState": "READY", "alias": [alias]}},
                    fetch=lambda url, h: (200, json.dumps({"apiUrl": "https://other/api"})))
    with pytest.raises(RuntimeError, match="another API"):
        bad.wire(dict(OUTPUTS), reapply=lambda o: OUTPUTS)


def test_note_line_without_a_record():
    assert W.note_line(None) == "UI preview: not recorded for this commit"
    assert W.note_line({"action": "skip", "note": "UI preview: skipped (commit author is the release bot)"}) == \
        "UI preview: skipped (commit author is the release bot)"


def test_preview_ui_declares_three_branch_scoped_preview_variables(monkeypatch):
    pulumi = pytest.importorskip("pulumi")             # the MR validate image has no Pulumi
    pytest.importorskip("pulumiverse_vercel")
    from types import SimpleNamespace
    from module11.infrastructure.harness import preview_ui

    made = []

    class Fake:
        def __init__(self, name, **kw):
            made.append((name, kw))

    monkeypatch.setattr("pulumiverse_vercel.ProjectEnvironmentVariable", Fake)
    monkeypatch.setattr(pulumi.Output, "from_input", staticmethod(lambda v: SimpleNamespace(apply=lambda f: f(v))))
    mr = SimpleNamespace(settings=SimpleNamespace(is_mr=True), name="mr-7")
    assert preview_ui.declare(mr, enabled=False) == {}
    preview_ui.declare(mr, enabled=True, api_url="https://api/x/", client_id="c", auth_url="https://auth",
                       project_id=PROJECT, team_id=TEAM, git_branch="friday-brevity")
    assert [n for n, _ in made] == ["ui-m11-api-url", "ui-m11-client-id", "ui-m11-auth-url"]
    assert all(kw["targets"] == ["preview"] and kw["git_branch"] == "friday-brevity" and kw["sensitive"] is False
               for _, kw in made)
    assert made[0][1]["value"] == "https://api/x"
    with pytest.raises(ValueError, match="gitBranch"):
        preview_ui.declare(mr, enabled=True, project_id=PROJECT, team_id=TEAM)
    assert preview_ui.urns("mr-7")[0] == \
        "urn:pulumi:mr-7::m11-harness::vercel:index/projectEnvironmentVariable:ProjectEnvironmentVariable::ui-m11-api-url"


def test_the_broker_returns_the_fields_the_decision_reads():
    from module11.lambdas.gitlab_notes import handler as H

    class GL:
        def commit(self, ref):
            return {"id": "c" * 40, "created_at": "t", "title": "x", "author_email": "presenter@example.com"}

        def merge_request(self, iid):
            return {"iid": iid, "state": "opened", "target_branch": "main", "source_branch": "b",
                    "source_project_id": 1, "target_project_id": 1, "sha": "c" * 40,
                    "author": {"id": 5, "username": "presenter"}}

        def merge_request_paths(self, iid):
            return []

    assert H.dispatch({"operation": "commit", "ref": "main"}, GL())["authorEmail"] == "presenter@example.com"
    assert H.dispatch({"operation": "merge_request", "iid": 3}, GL())["authorUsername"] == "presenter"
