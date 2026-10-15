# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`release deploy`'s mr-* UI hooks with a recording `pulumi`: a `wire` decision
sets the Vercel config and passes the token only to pulumi; `skip` sets nothing; a wired stack is not re-decided;
`--exclude-ui` passes the browser client, branding and Vercel URNs to `pulumi up --exclude`; destroy passes the
token when the stack has Vercel variables; dev/staging/prod never get UI hooks."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from module11.aws import deploy as AD
from module11.release import ui_preview as UI


class Session:
    role_arn = "arn:aws:iam::111122223333:role/m11-preview-deployer"

    def client(self, *a, **k):
        return SimpleNamespace()

    def env(self):
        return {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y", "AWS_SESSION_TOKEN": "z"}  # nosec B105 - dummy credentials in a fake session


class Ctx(AD.Context):
    def assume(self, role, stage, purpose, policy=None):
        return Session()


class FakeUi:
    def __init__(self, action):
        self.action, self.decided = action, []
        self.decision = None

    def predicted_origin(self):
        return "https://devops-companion-ui-git-b-t.vercel.app"

    def decide(self, already_wired):
        self.decided.append(already_wired)
        self.decision = UI.UiDecision(UI.WIRE) if self.action == "wire" or already_wired else UI.UiDecision(UI.SKIP, "bot")
        return self.decision

    def config(self):
        return {"enableVercel": "true", "vercelProjectId": "prj", "vercelTeamId": "team", "gitBranch": "b"}

    def env(self):
        return {"VERCEL_API_TOKEN": "secret-token"}  # nosec B105 - dummy token in a fake session

    def record(self, action, **fields):
        return {"action": action, "note": self.decision.note, **fields}


def deploy(tmp_path, stage="mr-7", ui=None, config=None, **kw):
    ctx = Ctx(root=tmp_path, account="111122223333", region="us-east-1", operators=[], log=lambda _: None,
              admin=SimpleNamespace())
    d = AD.Deploy(ctx, stage, ui=ui, **kw)
    calls = []

    def pulumi(*args, inputs_path=None, check=True, log_name=None):
        calls.append((args, dict(d.extra_env)))
        out = json.dumps(config or {}) if args[:1] == ("config",) and "--json" in args else "{}"
        # nosemgrep: dangerous-subprocess-use-audit - builds a fake CompletedProcess; nothing runs
        return subprocess.CompletedProcess(args, 0, out, "")
    d.pulumi = pulumi
    return d, calls


def test_wire_sets_the_vercel_config_and_the_token_reaches_only_pulumi(tmp_path):
    d, calls = deploy(tmp_path, ui=FakeUi("wire"))
    d._ui_prepare()
    sets = [a[4:] for a, _ in calls if a[:2] == ("config", "set")]
    assert sets == [("enableVercel", "true"), ("vercelProjectId", "prj"), ("vercelTeamId", "team"), ("gitBranch", "b")]
    assert d.extra_env == {"VERCEL_API_TOKEN": "secret-token"}  # nosec B105 - the fake session's dummy token
    assert d._env(None)["VERCEL_API_TOKEN"] == "secret-token"


def test_skip_sets_nothing_and_a_wired_stack_is_not_re_decided(tmp_path):
    ui = FakeUi("skip")
    d, calls = deploy(tmp_path, ui=ui)
    d._ui_prepare()
    assert not [a for a, _ in calls if a[:2] == ("config", "set")] and d.extra_env == {}
    assert d._ui_finish({}, None) == {"action": "skip", "note": "UI preview: skipped (commit author is the release bot)"}
    wired = FakeUi("skip")
    d2, _ = deploy(tmp_path, ui=wired, config={"m11-harness:uiPreview": {"value": "wired"}})
    d2._ui_prepare()
    assert wired.decided == [True] and d2._ui_wired is True


def test_exclude_ui_passes_the_ui_urns_to_pulumi_up(tmp_path):
    d, _ = deploy(tmp_path, exclude_ui=True)
    urns = d.ui_urns()
    assert urns[0].endswith("aws:cognito/userPoolClient:UserPoolClient::browser-client")
    assert urns[1].endswith("::browser-login-style") and len(urns) == 5
    assert all(u.startswith("urn:pulumi:mr-7::m11-harness::") for u in urns)


def test_only_mr_stacks_get_ui_hooks_or_run_as_the_caller(tmp_path):
    d, _ = deploy(tmp_path, stage="staging", ui=FakeUi("wire"), exclude_ui=True)
    assert d.ui is None and d.exclude_ui is False
    with pytest.raises(AD.DeployError):
        deploy(tmp_path, stage="prod", as_caller=True)
