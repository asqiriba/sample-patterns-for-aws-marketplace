# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Who gets a Vercel UI preview."""
from module11.release import ui_preview as U

BOT = U.Bot("project_12345678_bot_x", "project_12345678_bot_x@noreply.gitlab.example")
MEMBERS = [{"email": "Presenter@Example.com", "gitlab": {"login": "presenter"}},
           {"email": "quality@example.com", "gitlab": {"login": "QualityPerson"}}]


def test_bot_commit_is_skipped():
    d = U.decide({"author_email": BOT.commit_email.upper()}, "presenter", MEMBERS, BOT)
    assert d == U.UiDecision("skip", "bot")


def test_non_member_is_skipped():
    d = U.decide({"author_email": "someone@example.org"}, "someone", MEMBERS, BOT)
    assert d.action == "skip" and d.note == "UI preview: skipped (commit author is not a Vercel team member)"


def test_unreachable_vercel_is_skipped():
    assert U.decide({"author_email": "presenter@example.com"}, "presenter", None, BOT).note == \
        "UI preview: skipped (Vercel unavailable)"


def test_member_by_email_or_login_is_wired_case_insensitively():
    assert U.decide({"author_email": "presenter@EXAMPLE.com"}, "other", MEMBERS, BOT).action == "wire"
    assert U.decide({"author_email": "laptop@local"}, "qualityperson", MEMBERS, BOT).action == "wire"


def test_wired_stack_is_never_re_decided():
    assert U.decide({"author_email": BOT.commit_email}, "x", None, BOT, already_wired=True).action == "wire"


def test_fixed_note_lines():
    assert {r: U.UiDecision("skip", r).note for r in U.REASONS} == {
        "bot": "UI preview: skipped (commit author is the release bot)",
        "not_member": "UI preview: skipped (commit author is not a Vercel team member)",
        "unavailable": "UI preview: skipped (Vercel unavailable)",
        "blocked": "UI preview: skipped (Vercel blocked the commit author)",
    }
    assert U.blocked().reason == "blocked" and U.UiDecision("wire").note == "UI preview: wired"
