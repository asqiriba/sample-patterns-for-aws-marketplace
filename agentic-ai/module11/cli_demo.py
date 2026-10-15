# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 demo prep --baseline` : assert that prod is the demo baseline.

Checks, each printed with its evidence; exit 0 only when all pass:
1. prod has a `/current`, no rollout in progress, and the freeze is `none`;
2. `/applied` names `/current`'s manifest (Bake's prune ran) and the deployed pins grant exactly the release's own
   tool versions (no retained extra pins);
3. the release has its GitLab tag at `/current`'s commit and its package (`manifest.json`, `change-plan.json`);
4. prod's drift checker reports no finding.
The re-seal path (a release without its tag or package) is `demo prep --reset`'s; here a missing tag or package fails the
check. demo/commands.py adds the other `demo` commands (arm, rearm, prep --payments/--reset).
"""
from __future__ import annotations

import json
from pathlib import Path

from .release import naming


def _say(text: str = "", style: str | None = None) -> None:
    from .cli import say
    say(text, style)


def baseline_checks(*, current: dict | None, applied: dict | None, control: dict | None, freeze: dict,
                    pins: dict | None, tag: dict | None, package_files: list[str], drift_count: int | None) -> list:
    """[(name, ok, detail)] for `demo prep --baseline` (pure)."""
    from .release.drift import ACTIVE
    out = []
    if not current:
        return [("prod /current", False, "prod has no /current")]
    mv, dep = current["manifestVersion"], current["deployment"]["prod"]
    state = (control or {}).get("state", "IDLE")
    out.append(("prod release", state not in ACTIVE and freeze.get("state", "none") == "none",
                f"{dep.get('release')} ({mv}), rollout state {state}, freeze {freeze.get('state', 'none')}"))
    out.append(("applied", bool(applied) and applied.get("manifestVersion") == mv,
                f"/applied {applied.get('manifestVersion') if applied else None}"))
    own = ((pins or {}).get("own") or {}).get("toolArns") or {}
    granted = sorted(((pins or {}).get("granted") or {}).get("toolArns") or [])
    out.append(("pins", bool(own) and granted == sorted(own.values()),
                f"granted {len(granted)} tool version(s), own {len(own)}"))
    commit = (current.get("git") or {}).get("commit")
    out.append(("GitLab tag", bool(tag) and (tag.get("commit") or {}).get("id", "").startswith(str(commit or "")[:7])
                and bool(commit), f"{(tag or {}).get('name')} at {((tag or {}).get('commit') or {}).get('short_id')}"))
    out.append(("GitLab package", {"manifest.json", "change-plan.json"} <= set(package_files),
                f"files {', '.join(package_files) or 'none'}"))
    out.append(("drift", drift_count == 0, f"{drift_count} finding(s)"))
    return out


def cmd_demo_prep(args) -> int:
    from .demo import prep
    if prep.ensure_d_function(args.root):            # the presenter's `d N` function in .local/env.sh
        _say("module11/.local/env.sh: wrote the `d` function; run `source module11/.local/env.sh`")
    if not args.baseline:
        return prep.cmd(args)                        # --reset, --payments, --warm (demo/prep.py)
    from .aws import control_table, drift_reads, gitlab_api as G, params, releases
    from .cli_live import _context
    from .cli_ops import _invoke_drift
    import os
    ctx = _context(args.root)
    ssm = ctx.admin.client("ssm")
    current = params.stage_get(ssm, "prod", "current")
    applied = params.stage_get(ssm, "prod", "applied")
    control = control_table.get(ctx.admin.client("dynamodb"), naming.table("prod", "control"))
    freeze = params.freeze(ssm)
    pins = tag = None
    files: list[str] = []
    drift = None
    if current:
        dep = current["deployment"]["prod"]
        pins = json.loads(releases.get(ctx.admin.client("s3"), dep["pinsUri"]))
        gl = G.from_secret(ctx.admin.client("secretsmanager"),
                           os.environ.get("M11_GITLAB_TOKEN_SECRET", "devops-companion/partners/gitlab-token"),
                           os.environ.get("M11_GITLAB_PROJECT_ID", ""))
        name = (current.get("git") or {}).get("tag") or f"harness/{str(dep.get('release', '')).replace('harness-', '')}"
        tag = gl.tag(name)
        number = G.tag_number(name)
        files = gl.package_files(number) if number is not None else []
        drift = drift_reads.report_from(_invoke_drift(ctx, "prod")).drift_count
    rows = baseline_checks(current=current, applied=applied, control=control, freeze=freeze, pins=pins, tag=tag,
                           package_files=files, drift_count=drift)
    for name, ok, detail in rows:
        _say(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}", "green" if ok else "red")
    ok = all(r[1] for r in rows)
    _say(f"demo baseline: {'prod is the demo baseline' if ok else 'not ready'}", "green" if ok else "red")
    return 0 if ok else 1


def register(sub) -> None:
    demo = sub.add_parser("demo", help="demo preparation (prep --baseline|--reset|--payments|--warm, arm, rearm)")
    dsub = demo.add_subparsers(dest="demo_command", required=True)
    prep = dsub.add_parser("prep")
    mode = prep.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", action="store_true", help="assert prod's release, pins, tag, package and drift")
    mode.add_argument("--reset", action="store_true", help="the demo reset cycle, steps 1-10 (OPERATIONS.md)")
    mode.add_argument("--payments", action="store_true", help="the bot-authored payments MR (GitLab)")
    mode.add_argument("--warm", action="store_true", help="one invocation per endpoint, Logs Insights, tokens")
    prep.add_argument("--yes", action="store_true", help="--reset: run without the confirmation prompt")
    prep.set_defaults(fn=cmd_demo_prep)
    from .demo import commands
    commands.register(dsub)                          # demo arm --stage dev, demo rearm
