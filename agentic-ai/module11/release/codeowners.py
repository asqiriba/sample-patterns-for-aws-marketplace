# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""GitLab CODEOWNERS parsing and matching, as check C13 needs it.

Sections are `[Name][n] @owners` (optionally `^[Name]` for optional sections). Within a section
the last matching pattern wins; every section that matches a path applies. A pattern starting
with `/` is anchored at the project root, a trailing `/` matches a directory and everything in
it, `*` stays within one path segment and `**` crosses segments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SECTION = re.compile(r"^\^?\[(?P<name>[^\]]+)\](?:\[(?P<n>\d+)\])?\s*(?P<owners>.*)$")
GROUP_TOKEN = "@<group>/"  # nosec B105 - the CODEOWNERS group placeholder, not a credential
QUALITY_SECTION = "Quality"
# The committed template's quality line; `render(..., quality_users=...)` swaps its group for named users.
_QUALITY_LINE = re.compile(r"^(?P<head>\^?\[Quality\](?:\[\d+\])?)[ \t]+@<group>/quality[ \t]*$", re.M)
_USER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")


@dataclass
class Line:
    pattern: str
    owners: tuple[str, ...]


@dataclass
class Section:
    name: str
    approvals: int
    owners: tuple[str, ...]
    lines: list[Line] = field(default_factory=list)


def is_user(owner: str) -> bool:
    """`@alice` is a user owner; `@group/sub` is a group."""
    return owner.startswith("@") and "/" not in owner


def owner_leaf(owner: str) -> str:
    """`@<group>/quality` or `@<your-group>/quality` -> `quality`; a user stays as is."""
    return owner.rsplit("/", 1)[-1] if owner.startswith("@") and "/" in owner else owner


def parse(text: str) -> list[Section]:
    sections: list[Section] = [Section("<default>", 1, ())]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _SECTION.match(line)
        if m and line.lstrip("^").startswith("["):
            owners = tuple(o for o in m.group("owners").split() if o.startswith("@"))
            sections.append(Section(m.group("name"), int(m.group("n") or 1), owners))
            continue
        parts = line.split()
        owners = tuple(p for p in parts[1:] if p.startswith("@"))
        sections[-1].lines.append(Line(parts[0], owners or sections[-1].owners))
    return [s for s in sections if s.lines]


def _glob_regex(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            if pattern.startswith("/", i):
                i += 1
                out.append("(?:/|)")
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(pattern: str, path: str) -> bool:
    path = path.lstrip("/")
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    directory = body.endswith("/")
    body = body.rstrip("/")
    if not anchored:
        body = "**/" + body
    rx = _glob_regex(body)
    candidates = [path]
    parts = path.split("/")
    candidates += ["/".join(parts[:k]) for k in range(1, len(parts))]  # parent directories
    for candidate in candidates:
        if rx.match(candidate):
            if directory and candidate == path:
                continue  # a trailing-slash pattern matches only directories (so a parent)
            return True
    return False


def owners_for(sections: list[Section], path: str) -> dict[str, tuple[str, ...]]:
    """{section name: owners of the last matching line} for every section that matches."""
    out = {}
    for section in sections:
        hit = None
        for line in section.lines:
            if matches(line.pattern, path):
                hit = line
        if hit is not None:
            out[section.name] = hit.owners
    return out


def render(text: str, group: str, quality_users: tuple[str, ...] | list[str] = ()) -> str:
    """seed.py renders the literal `@<group>/` token from M11_GITLAB_GROUP.

    With `quality_users` (M11_QUALITY_USERS), the [Quality] section names those users instead of the `quality`
    group. GitLab can count a group Owner inherited from the parent group as eligible for a group section, so the
    user form is what keeps the quality reviewer a different person from the author's peers (apply after the
    10/13 recording)."""
    users = tuple(u.strip().lstrip("@") for u in quality_users if u and u.strip())
    if users:
        bad = [u for u in users if not _USER.match(u)]
        if bad:
            raise ValueError("invalid GitLab username in the quality users")
        text, n = _QUALITY_LINE.subn(lambda m: m.group("head") + " " + " ".join(f"@{u}" for u in users), text)
        if n != 1:
            raise ValueError("CODEOWNERS needs exactly one [Quality] section owned by @<group>/quality")
    return text.replace(GROUP_TOKEN, f"@{group}/")
