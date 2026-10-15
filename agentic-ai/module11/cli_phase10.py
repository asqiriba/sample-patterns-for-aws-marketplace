# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 teardown` and `module11 captures export`."""
from __future__ import annotations


def _say(text: str, style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def cmd_teardown(args) -> int:
    from . import teardown as TD
    from .cli_live import _context
    if not (args.dry_run or args.yes):
        raise ValueError("choose --dry-run (print the plan) or --yes (print the plan, then run it)")
    ctx = _context(args.root)
    plan = TD.plan_live(ctx, include_module10=args.include_module10, delete_gitlab_project=args.delete_gitlab_project)
    TD.render(plan, lambda text, style: _say(text, style))
    if args.dry_run:
        _say(f"dry run: {len(plan.items)} planned item(s), {len(plan.orphans)} ORPHAN? kept; nothing deleted", "green")
        return 0
    return TD.execute(ctx, plan, include_orphans=args.include_orphans, say=lambda text, style: _say(text, style))


def cmd_captures_export(args) -> int:
    from .demo import replay as RP
    written = RP.export(args.root)
    for path in written:
        _say(f"exported {path.relative_to(args.root)} (account IDs, ARNs, names and URL hosts redacted)")
    if not written:
        _say("no captures in module11/.local/captures; record them with `demos/module11_demo.py --record`", "yellow")
    return 0


def register(sub) -> None:
    td = sub.add_parser("teardown", help="delete only what this project owns ; --dry-run first")
    mode = td.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print steps 1-10 and every ORPHAN?; delete nothing")
    mode.add_argument("--yes", action="store_true", help="print the plan, then run it")
    td.add_argument("--include-orphans", action="store_true", help="also delete ORPHAN? items (typed confirmation)")
    td.add_argument("--include-module10", action="store_true", help="also tear down Module 10 (step 8)")
    td.add_argument("--delete-gitlab-project", action="store_true", help="delete instead of archive (O7)")
    td.set_defaults(fn=cmd_teardown)
    cap = sub.add_parser("captures", help="demo captures")
    csub = cap.add_subparsers(dest="captures_command", required=True)
    csub.add_parser("export", help="copy module11/.local/captures, redacted, into tests/fixtures/module11/captures") \
        .set_defaults(fn=cmd_captures_export)
