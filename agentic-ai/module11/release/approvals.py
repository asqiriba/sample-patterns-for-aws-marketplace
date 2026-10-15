# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The approvals a merge request needs by design, checked before the bot merges it.

GitLab's own `approved` / mergeable flag is necessary but not sufficient. A group Owner inherited from the
parent group can be eligible for a group-based code-owner section, so one person could satisfy both the
`[Quality]` section and its `[Quality-peer]` section (measured on GitLab.com during the build). The bot therefore
also checks, from the MR's changed paths and the CODEOWNERS file on the target branch:

- every matching code-owner section has its required count of approvals from that section's configured people
  (`M11_APPROVER_USERS`, the same user form `module11 partners up --approver-users` takes);
- a `quality` approval comes from someone who is neither a harness-owner nor on platform, so one person never
  counts for both sides of a quality change;
- the MR author's own approval never counts, and at least one other person approved.

Pure functions only; `module11.aws.bot_changes.wait_and_merge` calls `check` on every poll.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from . import codeowners

GROUPS = ("harness-owners", "platform", "quality")
PEERS = ("harness-owners", "platform")
USERNAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")


class ApproversNotConfigured(ValueError):
    """The bot cannot know who may approve, so it refuses to merge (fail closed)."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    missing: tuple[str, ...] = ()


def parse_approvers(text: str | None) -> dict[str, tuple[str, ...]]:
    """`{"harness-owners": ["a"], "platform": ["a"], "quality": ["b"]}` (M11_APPROVER_USERS) -> validated tuples."""
    if not text or not text.strip():
        raise ApproversNotConfigured("set M11_APPROVER_USERS (JSON usernames per group) so the bot can check the "
                                     "approvals the design requires before it merges")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApproversNotConfigured(f"M11_APPROVER_USERS is not JSON ({exc.msg})") from None
    if not isinstance(raw, dict):
        raise ApproversNotConfigured("M11_APPROVER_USERS must be a JSON object of group -> usernames")
    out = {}
    for group in GROUPS:
        names = raw.get(group) or []
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and USERNAME.match(n) for n in names):
            raise ApproversNotConfigured(f"M11_APPROVER_USERS names nobody valid for {group}")
        out[group] = tuple(names)
    overlap = set(out["quality"]) & (set(out["harness-owners"]) | set(out["platform"]))
    if overlap:
        raise ApproversNotConfigured(f"quality shares {', '.join(sorted(overlap))} with harness-owners or platform; "
                                     "a quality reviewer must be a different person")
    return out


def required_sections(codeowners_text: str, paths: Iterable[str]) -> dict[str, tuple[int, frozenset[str]]]:
    """{section: (approvals required, owner leaves)} over every changed path (a group leaf or a username)."""
    sections = codeowners.parse(codeowners_text)
    counts = {s.name: s.approvals for s in sections}
    out: dict[str, tuple[int, frozenset[str]]] = {}
    for path in paths:
        for name, owners in codeowners.owners_for(sections, path).items():
            leaves = frozenset(codeowners.owner_leaf(o).lstrip("@") for o in owners)
            if not leaves:
                continue
            _, have = out.get(name, (0, frozenset()))
            out[name] = (counts.get(name, 1), have | leaves)
    return out


def eligible(leaves: Iterable[str], approvers: Mapping[str, Sequence[str]]) -> frozenset[str]:
    """The people who may approve for these owners; a quality approver is never a harness-owner or platform member."""
    leaves = set(leaves)
    people = set()
    for leaf in leaves:
        people |= set(approvers.get(leaf) or ()) if leaf in GROUPS else {leaf}
    quality = set(approvers.get("quality") or ())
    # A `quality` group section, or a section rendered with the quality users (the user form): no peer counts.
    if "quality" in leaves or (people and people <= quality):
        people -= set().union(*(set(approvers.get(g) or ()) for g in PEERS))
    return frozenset(people)


def check(paths: Iterable[str], codeowners_text: str, approved_by: Iterable[str],
          approvers: Mapping[str, Sequence[str]], author: str | None) -> Verdict:
    """Whether the MR has every approval the design requires (the author's own approval never counts)."""
    approved = {u for u in approved_by if u} - {author or ""}
    missing = []
    if not approved:
        missing.append("no approval from anyone other than the author")
    for name, (need, leaves) in sorted(required_sections(codeowners_text, list(paths)).items()):
        people = eligible(leaves, approvers) - {author or ""}
        got = approved & people
        if len(got) < need:
            missing.append(f"[{name}] needs {need} approval(s) from {', '.join(sorted(people)) or 'nobody configured'}"
                           f" (has {len(got)})")
    return Verdict(not missing, tuple(missing))
