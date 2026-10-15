# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Who gets a Vercel UI preview. Pure.

On a Pro team Vercel deploys a private-repo commit only if its author is a team member, and the
bot authors every prep, reset, corpus, recommendation and scenario commit. Until the stack
records `uiPreview: wired`, `release deploy` calls `decide()` on every `mr-*` deploy:
- skip when the head commit's author email is the bot's commit email;
- skip when the commit email matches no member's email and the MR author's username matches
  no member's `gitlab.login` (both case-insensitive);
- skip when the Vercel members API is unreachable;
- otherwise wire. A wired stack is never re-decided.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

WIRE, SKIP = "wire", "skip"
REASONS = {
    "bot": "commit author is the release bot",
    "not_member": "commit author is not a Vercel team member",
    "unavailable": "Vercel unavailable",
    "blocked": "Vercel blocked the commit author",
}


@dataclass(frozen=True)
class Bot:
    login: str
    commit_email: str


@dataclass(frozen=True)
class UiDecision:
    action: str
    reason: str | None = None

    @property
    def note(self) -> str:
        """The fixed m11-gate note line."""
        if self.action == WIRE:
            return "UI preview: wired"
        return f"UI preview: skipped ({REASONS[self.reason]})"


def decide(head_commit: Mapping[str, Any], mr_author: str, members: Iterable[Mapping[str, Any]] | None,
           bot: Bot, already_wired: bool = False) -> UiDecision:
    """`members` is GET /v3/teams/{teamId}/members' list, or None when the API was unreachable."""
    if already_wired:
        return UiDecision(WIRE)
    email = str(head_commit.get("author_email") or "").lower()
    if email and email == bot.commit_email.lower():
        return UiDecision(SKIP, "bot")
    if members is None:
        return UiDecision(SKIP, "unavailable")
    login = (mr_author or "").lower()
    for member in members:
        member_email = str(member.get("email") or "").lower()
        member_login = str((member.get("gitlab") or {}).get("login") or "").lower()
        if (email and member_email == email) or (login and member_login == login):
            return UiDecision(WIRE)
    return UiDecision(SKIP, "not_member")


def blocked() -> UiDecision:
    """A predicted wire whose API-triggered deployment ends in Vercel's collaboration block."""
    return UiDecision(SKIP, "blocked")
