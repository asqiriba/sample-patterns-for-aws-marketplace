# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Governance fixes prepared for after the 10/13 recording (apply after the recording; nothing here runs live).

1. The [Quality] code-owner section and the production-gated quality rule name users, so an Owner inherited from
   the parent group cannot count as the quality reviewer.
2. The bot merges only when the approvals the design requires are there, not on GitLab's approved flag alone.
3. `partners up` sets and verifies the merge gate on the import path too.
No network: fake GitLab clients record the calls."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from module11.aws import bot_changes as B
from module11.release import approvals as AP
from module11.release import check as C
from module11.release import codeowners

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "module11/gitlab/CODEOWNERS").read_text(encoding="utf-8")
GROUP_FORM = codeowners.render(TEMPLATE, "example-group")
USER_FORM = codeowners.render(TEMPLATE, "example-group", ["quality-reviewer"])
APPROVERS = {"harness-owners": ("presenter",), "platform": ("presenter",), "quality": ("quality-reviewer",)}
EVAL = ["module11/harness/evaluation/thresholds.yaml"]
PROMPT = ["module11/harness/agent/prompts/companion.md"]


# ---------------------------------------------------------------- 1. user-form CODEOWNERS and approver rules
def test_quality_section_renders_with_named_users_and_still_passes_c13():
    sections = {s.name: s for s in codeowners.parse(USER_FORM)}
    assert sections["Quality"].owners == ("@quality-reviewer",)
    assert sections["Quality-peer"].owners == ("@example-group/harness-owners", "@example-group/platform")
    assert "@example-group/quality" not in USER_FORM and "@<group>/" not in USER_FORM
    assert C.c13(ROOT, USER_FORM, ".gitlab/CODEOWNERS") == []                 # every quality path keeps both sides
    peerless = USER_FORM.replace("[Quality-peer][1] @example-group/harness-owners @example-group/platform",
                                 "[Quality-peer][1] @quality-reviewer")
    assert any("quality path" in f.message for f in C.c13(ROOT, peerless, ".gitlab/CODEOWNERS"))


def test_quality_user_form_rejects_bad_usernames_and_a_template_without_the_line():
    with pytest.raises(ValueError):
        codeowners.render(TEMPLATE, "g", ["bad user"])
    with pytest.raises(ValueError):
        codeowners.render(TEMPLATE.replace("[Quality][1] @<group>/quality", "[Quality][1] @<group>/other"), "g", ["a"])
    assert codeowners.render(TEMPLATE, "g") == TEMPLATE.replace("@<group>/", "@g/")    # no users: unchanged form


def test_seed_renders_the_user_form_from_quality_users():
    from module11.gitlab import seed
    data = seed.content(ROOT, "module11/gitlab/CODEOWNERS", ".gitlab/CODEOWNERS", "example-group", ("quality-reviewer",))
    assert b"[Quality][1] @quality-reviewer\n" in data and b"@example-group/harness-owners" in data


def test_resolve_approvers_keeps_quality_as_users_even_when_groups_are_visible():
    from module11.cli_partners import resolve_approvers

    class GL:
        def project(self):
            return {"shared_with_groups": [{"group_full_path": f"g/{n}", "group_id": i}
                                           for n, i in (("harness-owners", 1), ("platform", 2), ("quality", 3))]}

        def get(self, path):
            name = path.split("username=")[1]
            return [{"username": name, "id": 70 + len(name)}]

    with pytest.raises(RuntimeError, match="quality approvers as users"):
        resolve_approvers(GL(), "g", {})
    out, form = resolve_approvers(GL(), "g", {"quality": ["quality-reviewer"]})
    assert form == "groups" and out["harness-owners"] == [{"group_id": 1}] and out["platform"] == [{"group_id": 2}]
    assert out["quality"] == [{"user_id": 70 + len("quality-reviewer")}]


# ---------------------------------------------------------------- 2. design approvals before a bot merge
def test_an_inherited_owner_never_counts_as_the_quality_reviewer():
    """MR 11's case: the presenter alone approved an evaluation change; GitLab called it mergeable."""
    for text in (GROUP_FORM, USER_FORM):
        verdict = AP.check(EVAL, text, ["presenter"], APPROVERS, author="m11-bot")
        assert not verdict.ok and any(m.startswith("[Quality] needs 1") for m in verdict.missing)
        ok = AP.check(EVAL, text, ["presenter", "quality-reviewer"], APPROVERS, author="m11-bot")
        assert ok.ok, ok.missing


def test_the_author_never_approves_their_own_change():
    verdict = AP.check(PROMPT, GROUP_FORM, ["presenter"], APPROVERS, author="presenter")
    assert not verdict.ok and "no approval from anyone other than the author" in verdict.missing
    assert AP.check(PROMPT, GROUP_FORM, ["presenter"], APPROVERS, author="m11-bot").ok


def test_approvers_configuration_fails_closed():
    with pytest.raises(AP.ApproversNotConfigured):
        AP.parse_approvers(None)
    with pytest.raises(AP.ApproversNotConfigured, match="different person"):
        AP.parse_approvers(json.dumps({"harness-owners": ["a"], "platform": ["a"], "quality": ["a"]}))
    with pytest.raises(AP.ApproversNotConfigured, match="nobody valid for quality"):
        AP.parse_approvers(json.dumps({"harness-owners": ["a"], "platform": ["a"], "quality": []}))
    assert AP.parse_approvers(json.dumps({k: list(v) for k, v in APPROVERS.items()})) == APPROVERS


class MergeGitLab:
    """An MR whose pipeline is green and which GitLab already reports `approved` after the presenter's click."""
    p = "/projects/1"

    def __init__(self, paths, approved_by, codeowners_text=GROUP_FORM):
        self.paths, self.approved_by, self.codeowners_text = paths, list(approved_by), codeowners_text
        self.merges = []

    def merge_request(self, iid):
        return {"iid": iid, "sha": "e" * 40, "target_branch": "main", "author": {"username": "m11-bot"},
                "head_pipeline": {"status": "success"}}

    def merge_request_paths(self, iid):
        return self.paths

    def file(self, path, ref):
        return self.codeowners_text.encode() if path == B.CODEOWNERS_PATH else None

    def get(self, path):
        assert path.endswith("/approvals")
        return {"approved": True, "approved_by": [{"user": {"username": u}} for u in self.approved_by]}

    def merge(self, iid, sha):
        self.merges.append((iid, sha))
        return {"merge_commit_sha": "f" * 40}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_wait_and_merge_waits_for_the_quality_reviewer_not_gitlabs_flag():
    gl, clock, said = MergeGitLab(EVAL, ["presenter"]), Clock(), []

    def sleep(s):
        clock.sleep(s)
        if clock.t >= 40:
            gl.approved_by.append("quality-reviewer")            # the second person approves later
    B.wait_and_merge(gl, 6, APPROVERS, poll=20, say=said.append, clock=clock, sleep=sleep)
    assert gl.merges == [(6, "e" * 40)] and clock.t >= 40          # merged only after the quality approval
    assert any("[Quality] needs 1" in line for line in said)


def test_wait_and_merge_gives_up_without_merging():
    gl, clock = MergeGitLab(EVAL, ["presenter"]), Clock()
    with pytest.raises(RuntimeError, match="approvals the design requires"):
        B.wait_and_merge(gl, 6, APPROVERS, wait_s=120, poll=20, say=lambda _: None, clock=clock, sleep=clock.sleep)
    assert gl.merges == []


def test_open_change_never_asks_gitlab_to_merge_on_its_own():
    import inspect
    assert "merge" not in inspect.signature(B.open_change).parameters


# ---------------------------------------------------------------- 3. merge settings on the import path
def test_merge_settings_are_set_and_read_back_when_an_import_left_them_unset():
    from module11 import cli_partners as CP

    class GL:
        p = "/projects/1"

        def __init__(self):
            self.state = {"only_allow_merge_if_pipeline_succeeds": True, "allow_merge_on_skipped_pipeline": None}
            self.puts = []

        def project(self):
            return dict(self.state)

        def put(self, path, body):
            self.puts.append(body)
            self.state.update(body)

    partners = CP.Partners.__new__(CP.Partners)
    partners.gl = GL()
    assert CP.merge_settings_drift(partners.gl.project()) == {"allow_merge_on_skipped_pipeline": False}
    assert partners.ensure_merge_settings() == "set allow_merge_on_skipped_pipeline"
    assert partners.gl.puts == [{"allow_merge_on_skipped_pipeline": False}]
    assert partners.ensure_merge_settings() == "ok" and len(partners.gl.puts) == 1
