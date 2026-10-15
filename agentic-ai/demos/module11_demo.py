# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
demos/module11_demo.py
======================
Module 11: Harness-based DevOps for agents, the v2 run-of-show: eight beats, 17:15
nominal, edits of 15:15 (cuts C1-C5) and 11:55 (C1-C8), exactly one live race (the prod canary of the payments
release, B5). The presenter types `d N` (a shell function `demo prep` writes into module11/.local/env.sh).

Usage:
    python demos/module11_demo.py --replay-fixtures --no-pause     # no AWS account: the committed, redacted captures
    python demos/module11_demo.py --section 5 --cut full           # one beat (d 5)
    python demos/module11_demo.py --no-pause --pace budget --record --canary-stage dev   # the automated dry run
    python demos/module11_demo.py --replay-fixtures --no-pause --explain   # audience panels on (or M11_EXPLAIN=1)

Beats (--section N):
    0  B0 Context                                    0:45
    1  B1 The harness, declared in code               2:00   1c = C5
    2  B2 The manifest and drift                      2:15   2d-2e = C6
    3  B3 Friday, four words: the merge request       3:00   3c = C4
    4  B4 Designing the gate                          1:30   4d = C3, 4a-4c = C8
    5  B5 Canary and automatic rollback (LIVE RACE)   5:30   none
    6  B6 Coordination and closing the loop           1:45   6d = C1, 6e = C2, 6b-6c = C7
    7  B7 Wrap                                        0:30

Keys at every pause: Enter = next; s = skip (cuttable sub-beats only); r = replay this beat from its capture.
B5 only: t = `module11 rollout tick`; v = the verdict, accepted only after F5 and once ROLLED_BACK.
Everything not live prints `RAN BEFORE THE SESSION: real run <id> at <ts>`; a replay runs under the yellow
banner `REPLAY of real run <ids>, recorded <timestamp>`. Every run writes module11/.local/timings/<run>.json.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from module11.demo import display as DSP  # noqa: E402
from module11.demo import explain as EX  # noqa: E402
from module11.demo import replay as RP  # noqa: E402
from module11.demo import race  # noqa: E402
from module11.demo import state as S  # noqa: E402

LOCAL = ROOT / "module11/.local"
PY = sys.executable
RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}")


@dataclass(frozen=True)
class Sub:
    id: str
    title: str
    budget: int                     # seconds
    cut: str | None = None          # cut ID (d)
    kind: str = "L"                 # L live, P ran before the session
    say: str = ""                   # the presenter's line (printed only with --notes)


SUBBEATS: dict[int, list[Sub]] = {
    0: [Sub("0a", "module11 status --all", 45, say="Shipping an agent once is easy. Module 11 is about shipping it "
            "the fiftieth time without fear. Something is already waiting in production.")],
    1: [Sub("1a", "module11 tree", 20), Sub("1b", "infrastructure/harness/tools.py", 30),
        Sub("1c", "pulumi preview --stack prod --diff --policy-pack", 35, "C5"),
        Sub("1d", "toolPolicies: search_runbooks", 10),
        Sub("1e", "module11 resources --stage prod --kind guardrail,identity,deployer", 25,
            say="Three stacks in one account today; one account per stage in yours.")],
    2: [Sub("2a", "module11 manifest build", 20), Sub("2b", "--explain-normalization", 15),
        Sub("2c", "declared vs. deployed", 25), Sub("2d", "the unreviewed hotfix, then drift check", 30, "C6"),
        Sub("2e", "module11 drift revert --stage staging", 45, "C6")],
    3: [Sub("3a", "the Friday commit", 20, kind="P"), Sub("3b", "module11 detect --base main", 20),
        Sub("3c", "consistency check, both directions", 30, "C4"), Sub("3d", "the GitLab MR", 40, kind="P"),
        Sub("3e", "the Vercel preview: one live agent run", 60), Sub("3f", "takeaway", 10)],
    4: [Sub("4a", "incident-triage-014 and requiredTools", 25, "C8"), Sub("4b", "thresholds.yaml", 25, "C8"),
        Sub("4c", "module11 calibration show", 25, "C8", "P"), Sub("4d", "module11 evalset show v3", 15, "C3", "P")],
    5: [Sub("5a", "module11 rollout show", 20), Sub("F1", "CodePipeline: the payments release", 35, kind="P"),
        Sub("F2", "GitLab: environment, package, tag", 35, kind="P"), Sub("F3", "module11 versions", 25),
        Sub("F4", "shadow report, model compare, A/B", 45, kind="P"), Sub("F5", "flags kill", 15),
        Sub("F6", "the controller's tick table", 35), Sub("5b", "verdict", 40),
        Sub("5c", "side effects and memory purge", 65,
            say="Memory indexes writes a little after they land, so the purge counts what the canary actually wrote "
                "and waits until every one is gone. Rollback doesn't undo side effects; you reconcile them."),
        Sub("5d", "takeaway", 15)],
    6: [Sub("6a", "module11 scenario add --from-session", 15), Sub("6b", "drift check --external", 25, "C7"),
        Sub("6c", "cost per successful session", 25, "C7"), Sub("6d", "corpus publish", 20, "C1"),
        Sub("6e", "recommendation show and import", 20, "C2", "P")],
    7: [Sub("7a", "wrap", 30)],
}
TITLES = {0: "B0 Context", 1: "B1 The harness is the release unit, declared in code",
          2: "B2 The manifest: hash, declared vs. deployed, drift", 3: "B3 Friday, four words: the merge request",
          4: "B4 Designing the gate", 5: "B5 The one that got through: canary and automatic rollback (LIVE RACE)",
          6: "B6 Coordination and closing the loop", 7: "B7 Wrap"}
CUTS = {"C1": ("6d",), "C2": ("6e",), "C3": ("4d",), "C4": ("3c",), "C5": ("1c",), "C6": ("2d", "2e"),
        "C7": ("6b", "6c"), "C8": ("4a", "4b", "4c")}
CUT_LEVELS = {"full": (), "15": ("C1", "C2", "C3", "C4", "C5"), "12": tuple(f"C{i}" for i in range(1, 9))}
KEYS = {"Enter": "next", "s": "skip a cuttable sub-beat", "r": "replay this beat",
        "t": "B5: module11 rollout tick", "v": "B5: the verdict (after F5, once ROLLED_BACK)"}
T_PROMPT_S, STOP_S = 270, 330            # t0 + 4:30 and t0 + 5:30 (c.5 steps 3 and 4)
TRAFFIC = ["--sessions", "180", "--concurrency", "20", "--mix", "payments=0.6"]


def skipped(cut: str) -> set[str]:
    return {s for c in CUT_LEVELS[cut] for s in CUTS[c]}


def budget(section: int, cut: str = "full") -> int:
    gone = skipped(cut)
    return sum(s.budget for s in SUBBEATS[section] if s.id not in gone)


class Replay(Exception):
    """`r` at a pause: replay this beat from its capture."""


class StopTake(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- the run
class Run:
    def __init__(self, args, section: int, screen: DSP.Screen, timings: dict) -> None:
        self.args, self.section, self.screen, self.t = args, section, screen, timings
        self.skips = skipped(args.cut)
        self.clock = time.time
        self.stage = args.canary_stage
        self._ctx = None
        self.notes: dict = {}
        self.explain = bool(getattr(args, "explain", False))
        self.explain_deferred: list[str] = []      # concept keys held back while B5's live footer redraws
        if self.explain and screen.clean is None:  # audience-clean labels; the recording keeps the raw lines
            screen.clean = EX.audience_line

    # -- explain mode (display only: never recorded, never changes what runs)
    def _active_ids(self) -> list[str]:
        return [s.id for s in SUBBEATS[self.section] if s.id not in self.skips]

    def _explain(self, sub: Sub) -> None:
        footer = self.notes.get("footer")
        if footer is not None and footer.live_active:
            concept = EX.EXPLAIN[sub.id].concept if sub.id in EX.EXPLAIN else None
            if concept:
                self.explain_deferred.append(concept)
            return
        block = EX.sub_block(sub.id, self._active_ids())
        if block is not None:
            self.screen.show(block)

    def explain_race_preview(self) -> None:
        if self.explain:
            self.screen.show(EX.race_preview())

    # -- input
    def key(self, prompt: str, allowed: str = "sr") -> str:
        if self.args.no_pause:
            return ""
        if self.explain:
            prompt = EX.audience_prompt(prompt)
        while True:
            try:
                k = input(prompt).strip().lower()
            except EOFError:
                return ""
            if k == "" or k in allowed:
                return k
            self.screen.say(f"keys here: Enter{', ' + ', '.join(allowed) if allowed else ''}", "dim")

    # -- sub-beats
    def step(self, sub: Sub, fn: Callable[[], None]) -> None:
        if sub.id in self.skips:
            self._skip(sub, f"cut {sub.cut} (level {self.args.cut})")
            return
        k = self.key(f"[{sub.id}] {sub.title}{' (' + sub.cut + ')' if sub.cut else ''}: Enter, "
                     f"{'s skip, ' if sub.cut else ''}r replay > ", "sr" if sub.cut else "r")
        if k == "s":
            self._skip(sub, "s")
            return
        if k == "r":
            raise Replay()
        started = self.clock()
        self.screen.sub = sub.id
        if self.explain:
            self._explain(sub)
        self.screen.say(f"[{sub.id}] {sub.title}" + (f"   ({sub.cut})" if sub.cut else ""), "bold")
        if self.args.notes and sub.say:
            self.screen.say(f"  say: {sub.say}", "dim")
        try:
            fn()
        finally:
            if self.args.pace == "budget":
                remaining = sub.budget - (self.clock() - started)
                if remaining > 0:
                    time.sleep(remaining)
            self.t["subBeats"].append({"id": sub.id, "section": self.section, "cut": sub.cut, "budget": sub.budget,
                                       "start": DSP.now_iso(), "seconds": round(self.clock() - started, 1),
                                       "skipped": False})

    def _skip(self, sub: Sub, why: str) -> None:
        self.screen.say(f"[{sub.id}] skipped: {why}", "dim")
        self.t["subBeats"].append({"id": sub.id, "section": self.section, "cut": sub.cut, "budget": sub.budget,
                                   "skipped": True, "reason": why})
        self.t["cutPoints"].append({"id": sub.id, "cut": sub.cut, "at": DSP.now_iso(), "reason": why})

    def event(self, name: str, **fields) -> None:
        self.t["events"].append({"event": name, "at": DSP.now_iso(), **fields})

    # -- commands
    def cmd(self, *argv: str, root: Path | None = None, show: Callable[[str], bool] | None = None,
            timeout: int = 600, expect: int | None = None) -> tuple[int, str]:
        """Run `python -m module11 ...` and stream its output through the screen. `expect`: the exit code the beat
        demonstrates (3c's checks fail on purpose), printed as expected rather than as a replay prompt."""
        root = root or ROOT
        self.screen.say(f"$ module11 {' '.join(argv)}", "bold green", kind="cmd")
        try:
            # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI with the beat's fixed arguments
            proc = subprocess.Popen([PY, "-m", "module11", "--root", str(root), *argv], cwd=ROOT, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        except OSError as exc:
            self.screen.say(f"cannot run module11: {exc}", "red")
            return 127, ""
        lines = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if show is None or show(line):
                self.screen.say(line)
        code = proc.wait(timeout=timeout)
        if expect is not None and code == expect:
            self.screen.say(f"(exit {code}, as expected: the check refuses the edit)", "dim")
        elif code:
            self.screen.say(f"(exit {code}; press r to replay this beat)", "yellow")
        return code, "\n".join(lines)

    def file(self, rel: str, start: str | None = None, lines: int = 40) -> None:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8").splitlines()
        begin = next((i for i, line in enumerate(text) if start and start in line), 0)
        self.screen.say(f"--- {rel}", "bold")
        for line in text[begin:begin + lines]:
            self.screen.say(line)

    @property
    def ctx(self):
        if self._ctx is None:
            from module11.cli_live import _context
            self._ctx = _context(ROOT)
        return self._ctx


# ---------------------------------------------------------------- live reads shared by beats
def _control(run: Run, stage: str) -> dict:
    from module11.aws import control_table
    from module11.release import naming
    return control_table.get(run.ctx.admin.client("dynamodb"), naming.table(stage, "control")) or {}


def _arm_counts(run: Run, stage: str, rid: str) -> dict:
    from module11.cli_ops import _arm_counts as counts
    return counts(run.ctx, stage, rid)


def _armed(run: Run) -> dict:
    return S.read().get("armed" if run.stage == "prod" else "devArmed") or {}


def _production_rows(run: Run, execution_id: str | None) -> list[dict]:
    from module11.demo import rearm as R
    if not execution_id:
        return []
    return R.actions(run.ctx.admin.client("codepipeline"), execution_id)


def bake_line(run: Run) -> str:
    from module11.demo import rearm as R
    armed = S.read().get("armed") or {}
    if run.stage != "prod":
        return "Bake: dev rehearsal, operator deploy (no pipeline execution); prod's Bake prunes and posts its note"
    try:
        status = R.action_status(_production_rows(run, armed.get("pipelineExecutionId")), "Bake")
    except Exception as exc:  # noqa: BLE001
        return f"Bake status unreadable ({type(exc).__name__})"
    line = f"Bake: {status}. Bake prunes prod's retained pins and posts its note before the pipeline turns FAILED"
    return line + ("; tab 3 shows it" if status == "Failed" else "")


def _label_execution(run: Run, execution_id: str | None) -> None:
    if not execution_id:
        return
    try:
        ex = run.ctx.admin.client("codepipeline").get_pipeline_execution(
            pipelineName="m11-release", pipelineExecutionId=execution_id)["pipelineExecution"]
        started = next((v for k, v in ex.items() if k.lower().endswith("time")), None)
        run.screen.ran_before(execution_id, str(started or ex.get("status")))
    except Exception:  # noqa: BLE001
        run.screen.ran_before(execution_id, "see CodePipeline")


# ---------------------------------------------------------------- B0
def beat0(run: Run) -> None:
    def a() -> None:
        _, out = run.cmd("status", "--all")
        if run.explain:                  # the same status lines as a table with a legend (display only)
            view = EX.status_view(out.splitlines())
            if view is not None:
                run.screen.show(view)
    run.step(SUBBEATS[0][0], a)


# ---------------------------------------------------------------- B1
def _retake_check_1c(run: Run) -> bool:
    """b.1 re-take order: before 1c, offer `r` if m11-deploy is InProgress, or Bake is InProgress while the rollout
    is ROLLED_BACK or PROMOTED (its prune), or either read fails. WARMING with Bake InProgress is normal."""
    from module11.demo import rearm as R
    try:
        rows = _production_rows(run, (S.read().get("armed") or {}).get("pipelineExecutionId"))
        state = _control(run, "prod").get("state")
    except Exception as exc:  # noqa: BLE001
        run.screen.say(f"1c re-take check failed ({type(exc).__name__}: {exc}); use r for 1c", "yellow")
        return False
    deploy, bake = R.action_status(rows, "Deploy"), R.action_status(rows, "Bake")
    if deploy == "InProgress" or (bake == "InProgress" and state in ("ROLLED_BACK", "PROMOTED")):
        run.screen.say(f"a pulumi up can hold the prod stack (Deploy {deploy}, Bake {bake}, rollout {state}); "
                       "use r for 1c", "yellow")
        return False
    return True


def _pulumi_preview(run: Run) -> None:
    if not _retake_check_1c(run):
        if run.key("r replays 1c from its capture > ", "r") == "r":
            raise Replay()
        return
    from module11.aws import deploy as AD, params, releases
    deploy = AD.Deploy(run.ctx, "prod")
    applied = params.stage_get(deploy.ssm, "prod", "applied")
    data = releases.get_verified(deploy.s3, applied["releaseInputsUri"], applied["sha256"], "applied inputs")
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as tmp:
        tmp.write(data)
    run.screen.say("$ pulumi preview --stack prod --diff --policy-pack infrastructure/policy", "bold green", kind="cmd")
    try:
        out = deploy.pulumi("preview", "--stack", run.ctx.stack("prod"), "--diff", "--policy-pack", "../policy",
                            "--non-interactive", inputs_path=Path(tmp.name), check=False)
    finally:
        os.unlink(tmp.name)
    for line in (out.stdout + out.stderr).splitlines()[-25:]:
        run.screen.say(line)


def _tool_policies(run: Run) -> None:
    """1d: the deployed search_runbooks role's own policy, read from IAM (what the tool can actually do). The
    harness stack exports no per-tool policy, so an earlier version printed `null` here."""
    from module11.release import naming
    role = naming.role("prod", "search_runbooks")
    run.screen.say(f"$ aws iam get-role-policy --role-name {role} --policy-name tool-search_runbooks", "bold green",
                   kind="cmd")
    doc = run.ctx.admin.client("iam").get_role_policy(RoleName=role, PolicyName="tool-search_runbooks")["PolicyDocument"]
    run.screen.api("toolPolicies.search_runbooks", doc)
    run.screen.say(json.dumps(doc, indent=2, default=str))


def beat1(run: Run) -> None:
    s = {x.id: x for x in SUBBEATS[1]}
    run.step(s["1a"], lambda: run.cmd("tree"))
    run.step(s["1b"], lambda: run.file("module11/infrastructure/harness/tools.py", "def ", 40))
    run.step(s["1c"], lambda: _pulumi_preview(run))
    run.step(s["1d"], lambda: _tool_policies(run))

    def e() -> None:
        run.cmd("resources", "--stage", "prod", "--kind", "guardrail,identity,deployer")
        run.screen.say("Today dev, staging and prod are three stacks in one account. In your organization: one "
                       "account per stage (docs/MULTI_ACCOUNT.md).", "bold yellow")
    run.step(s["1e"], e)


# ---------------------------------------------------------------- B2
def _scratch_copy(src: Path, dest: Path) -> None:
    """A copy of the module11 tree for 2b's scratch edit. Symlinks stay symlinks: the Pulumi projects' `venv` links
    point at a whole virtual environment, and following them copied about 56,000 files (2b took 159 s, not 15 s)."""
    import shutil
    shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(".local", "__pycache__"))


def _normalization(run: Run) -> None:
    from module11.demo import wording
    with tempfile.TemporaryDirectory(prefix="m11-norm-") as tmp:
        scratch = Path(tmp) / "agentic-ai"
        _scratch_copy(ROOT / "module11", scratch / "module11")
        for extra in ("module10", "module7"):
            if (ROOT / extra).exists():
                (scratch / extra).symlink_to(ROOT / extra)
        prompt = scratch / wording.COMPANION_PROMPT
        prompt.write_bytes(prompt.read_text(encoding="utf-8").replace("\n", " \r\n").encode("utf-8"))
        run.screen.say("companion.md now has a trailing space on every line and CRLF line endings (scratch copy)",
                       "yellow")
        run.cmd("manifest", "build", "--explain-normalization", root=scratch,
                show=lambda line: "Digest" in line or "companion" in line or line.startswith("manifestVersion"))


def _drift_hotfix(run: Run) -> None:
    from module11.aws import params
    from module11.demo import locks as L
    lock = L.read("staging")
    if lock is not None:
        run.screen.say("staging A/B in progress; 2d-2e skipped", "yellow")
        run.event("2d-2e skipped", reason="staging.lock", lock=lock.message())
        run.notes["revertSkipped"] = True
        return
    old = race.retake_check(clock=time.time, sleep=time.sleep)
    if old is not None:
        run.screen.say("staging revert still running; 2d-2e skipped", "yellow")
        race.write_drift_revert(pid=old.get("pid"), log=old.get("log"), started_at=old.get("startedAt"),
                                backgrounded=False)
        run.notes["revertSkipped"] = "kept"
        return
    current = params.stage_get(run.ctx.admin.client("ssm"), "staging", "current")
    dep = current["deployment"]["staging"]
    declared = int(dep["endpoints"]["staging"])
    older = declared - 1
    runtime_id = str(dep["runtimeId"])
    if not RUNTIME_ID.fullmatch(runtime_id):        # the record's value becomes an aws CLI argument
        run.screen.say("staging's runtime ID is not a plain AgentCore runtime ID; 2d skipped", "red")
        return
    cmd = ["aws", "bedrock-agentcore-control", "update-agent-runtime-endpoint", "--agent-runtime-id",
           runtime_id, "--endpoint-name", "staging", "--agent-runtime-version", str(older)]
    run.screen.say("$ " + " ".join(cmd[:3]) + " … --endpoint-name staging --agent-runtime-version " + str(older),
                   "bold green", kind="cmd")
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; aws CLI with the runtime ID checked above and an int version
    out = subprocess.run(cmd, capture_output=True, text=True)
    run.screen.say((out.stdout or out.stderr).strip()[:300] or f"exit {out.returncode}")
    run.cmd("drift", "check", "--stage", "staging")


def _drift_revert(run: Run) -> None:
    if run.notes.get("revertSkipped"):
        run.screen.say("2e skipped with 2d", "dim")
        if run.notes["revertSkipped"] is True:
            race.write_drift_revert(pid=None, log=None, started_at=None, backgrounded=False)
        return
    LOCAL.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = LOCAL / f"drift-revert-{stamp}.log"
    run.screen.say("$ module11 drift revert --stage staging", "bold green", kind="cmd")
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; the fixed drift-revert command through run_logged
    child = subprocess.Popen([PY, "-m", "module11.demo.run_logged", "--log", str(log), "--", PY, "-m", "module11",
                              "drift", "revert", "--stage", "staging"], cwd=ROOT, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = DSP.now_iso()
    race.write_drift_revert(pid=child.pid, log=str(log), started_at=started, backgrounded=False)
    shown, end = 0, time.time() + race.REVERT_FOREGROUND_S
    while time.time() < end:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        lines = text.splitlines()
        for line in lines[shown:]:
            if not line.startswith("M11_EXIT"):
                run.screen.say(line)
        shown = len(lines)
        if race.log_exit(str(log)) is not None:
            run.event("drift revert finished", exit=race.log_exit(str(log))[0], foreground=True)
            return
        time.sleep(1)
    race.set_backgrounded()
    run.screen.say(f"revert continuing in background; log {log.relative_to(ROOT)}", "yellow")
    run.event("drift revert backgrounded", log=str(log.relative_to(ROOT)), pid=child.pid, cut="C6")


def beat2(run: Run) -> None:
    s = {x.id: x for x in SUBBEATS[2]}
    run.step(s["2a"], lambda: run.cmd("manifest", "build"))
    run.step(s["2b"], lambda: _normalization(run))

    def c() -> None:
        from module11.aws import params
        current = params.stage_get(run.ctx.admin.client("ssm"), "prod", "current") or {}
        tag = (current.get("git") or {}).get("tag") or "harness/v?"
        clone = LOCAL / "harness-clone"
        run.cmd("manifest", "diff", "--declared", tag, "--deployed", "prod", root=clone)
        run.cmd("manifest", "diff", "--declared", "origin/main", "--deployed", "prod", root=clone)
    run.step(s["2c"], c)
    run.step(s["2d"], lambda: _drift_hotfix(run))
    run.step(s["2e"], lambda: _drift_revert(run))
    last = next((b for b in reversed(run.t["subBeats"]) if b["id"] == "2e" and b["section"] == 2), {})
    if last.get("skipped"):              # M11_CUT=12 or s: a fresh record with no revert (pid, log, startedAt null)
        race.write_drift_revert(pid=None, log=None, started_at=None, backgrounded=False)


# ---------------------------------------------------------------- B3
def _friday(run: Run) -> dict | None:
    doc = S.read()
    return doc.get("fridayMr") or (doc.get("fallbackMr") if isinstance(doc.get("fallbackMr"), dict) else None)


def beat3(run: Run) -> None:
    s = {x.id: x for x in SUBBEATS[3]}
    friday = _friday(run)
    if not friday:
        run.screen.say("B3 needs the Friday MR: the GitLab MR flow and the Vercel preview are not configured yet "
                       "(PARTNER_SETUP.md sets them up). This path renders at its 3:00 budget; the timing counts as budget, "
                       "not measured.", "yellow")
        run.event("B3 not configured", budgetSeconds=budget(3, run.args.cut))
        for sub in SUBBEATS[3]:
            run.step(sub, lambda sub=sub: run.screen.say(f"  {sub.id}: not configured (PARTNER_SETUP.md)", "dim"))
        return
    sha = friday.get("sha", "HEAD")
    clone = _friday_tree(friday)
    from module11.demo import live_check
    gl = live_check._gitlab_client()

    def a() -> None:
        out = subprocess.run(["git", "-C", str(clone), "show", "--format=%H%n%an <%ae>  %cI%n%s", sha, "--",
                              "module11/harness/agent/prompts/companion.md"], capture_output=True, text=True)
        for line in out.stdout.splitlines()[:14]:
            run.screen.say(line)
        sig = gl.commit_signature(sha) or {}
        run.screen.api("gitlab.commit_signature", {k: sig.get(k) for k in ("signature_type", "verification_status")})
        run.screen.say(f"GitLab: {sig.get('signature_type', 'no')} signature, {sig.get('verification_status', 'unsigned')}")
        run.screen.ran_before(f"commit {sha[:12]}, signed", friday.get("pushedAt", "?"))
    run.step(s["3a"], a)
    run.step(s["3b"], lambda: run.cmd("detect", "--base", "origin/main", root=clone))

    def c() -> None:
        import shutil
        with tempfile.TemporaryDirectory(prefix="m11-check-") as tmp:
            scratch = Path(tmp) / "clone"
            shutil.copytree(clone, scratch, ignore=shutil.ignore_patterns(".git"))
            prompt = scratch / "module11/harness/agent/prompts/companion.md"
            text = prompt.read_text(encoding="utf-8")
            prompt.write_text(text.replace("`search_runbooks`", "the runbook tool"), encoding="utf-8")
            run.screen.say("edit 1 (scratch copy): remove the `search_runbooks` mention", "yellow")
            run.cmd("check", root=scratch, show=lambda line: "C5" in line or "checks:" in line, expect=1)
            prompt.write_text(text + "\nUse `restart_service` when a restart helps.\n", encoding="utf-8")
            run.screen.say("edit 2 (scratch copy): mention `restart_service`", "yellow")
            run.cmd("check", root=scratch, show=lambda line: "C4" in line or "checks:" in line, expect=1)
        run.screen.say("both edits discarded; nothing can reach a commit", "dim")
    run.step(s["3c"], c)

    def d() -> None:
        mr = gl.merge_request(int(friday["iid"]))
        run.screen.api("gitlab.merge_request", {k: mr.get(k) for k in ("iid", "title", "state", "sha",
                                                                        "merge_status", "detailed_merge_status")})
        run.screen.say(f"!{mr['iid']} {mr['title']}: {mr.get('detailed_merge_status')}")
        for rule in gl.approval_state(int(friday["iid"])):
            label = f"[{rule['section']}] {rule['name']}" if rule["section"] else rule["name"]
            run.screen.say(f"  approval {label}: {'satisfied' if rule['approved'] else 'required'}"
                           + (f" by {', '.join(rule['approvedBy'])}" if rule["approvedBy"] else ""))
        pipe = mr.get("head_pipeline") or {}
        for job in gl.pipeline_jobs(pipe["id"]) if pipe.get("id") else []:
            run.screen.say(f"  {job['name']:<20} {job['status']}")
        for line in _gate_note_lines(gl, int(friday["iid"])):
            run.screen.say(line)
        run.screen.ran_before(f"MR !{mr['iid']} pipeline {pipe.get('id')}", str(pipe.get("created_at")))
    run.step(s["3d"], d)
    run.step(s["3e"], lambda: run.screen.say(f"open the branch-alias link in the m11-gate note (tab 2) and click "
                                             f"the Example; {friday.get('alias', '')}", "bold"))
    run.step(s["3f"], lambda: run.screen.say("Same answer, wrong path: the trajectory gate blocked the merge.",
                                             "bold yellow"))


def _friday_tree(friday: dict) -> Path:
    """A detached checkout of the Friday commit beside the harness clone (3a-3c read it; nothing is pushed)."""
    clone, tree, sha = LOCAL / "harness-clone", LOCAL / "friday-tree", friday.get("sha", "HEAD")
    subprocess.run(["git", "-C", str(clone), "fetch", "-q", "origin", friday.get("branch", "friday-brevity"), "main"],
                   check=True, capture_output=True)
    if not (tree / ".git").exists():
        subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(tree), sha], check=True,
                       capture_output=True)
    else:
        subprocess.run(["git", "-C", str(tree), "checkout", "-q", "--detach", sha], check=True, capture_output=True)
    return tree


def _gate_note_lines(gl, iid: int) -> list[str]:
    """The m11-gate note's verdict, its FAIL rows and its UI line, as the note shows them."""
    from module11.cli_preview import MARKER
    notes = [n for n in gl.paginate(f"{gl.p}/merge_requests/{iid}/notes?sort=asc") if MARKER in (n.get("body") or "")]
    if not notes:
        return ["  m11-gate note: not posted yet"]
    out = []
    for line in notes[-1]["body"].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(cells) >= 4 and cells[2] in ("FAIL", "INCONCLUSIVE"):
            out.append(f"  {cells[0]} ({cells[1]}): {cells[2]}, {cells[3]}")
        elif line.startswith("**Gate:") or line.startswith("UI preview:"):
            out.append("  " + line)
    return out


# ---------------------------------------------------------------- B4
def beat4(run: Run) -> None:
    s = {x.id: x for x in SUBBEATS[4]}

    def a() -> None:
        path = ROOT / "module11/harness/evaluation/scenarios"
        for f in sorted(path.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if "incident-triage-014" in line:
                    doc = json.loads(line)
                    for key in ("scenario_id", "expected_trajectory", "assertions", "expected_response"):
                        run.screen.say(f"{key}: {json.dumps(doc.get(key))[:240]}")
        run.file("module11/harness/agent/orchestration.yaml", "requiredTools", 4)
    run.step(s["4a"], a)

    def b() -> None:
        run.file("module11/harness/evaluation/thresholds.yaml", None, 30)
        run.screen.say("Latency is compared against the current version measured at the same time, so a slow "
                       "provider doesn't fail a good release.", "bold")
        mr = S.read().get("qualityApprovedMr")
        if mr:
            run.screen.ran_before(f"quality-approved MR !{mr}", "see GitLab")
    run.step(s["4b"], b)
    run.step(s["4c"], lambda: run.cmd("calibration", "show"))
    run.step(s["4d"], lambda: run.cmd("evalset", "show"))


# ---------------------------------------------------------------- B5
class Footer:
    """Polls the control item and outcome items read-only every 5 s, checks the traffic child at every poll,
    and redraws at 1 Hz (rich Live) unless --no-live-footer."""

    def __init__(self, run: Run, rid: str, t0: float, child) -> None:
        self.run, self.rid, self.t0, self.child = run, rid, t0, child
        self.item: dict = {}
        self.counts: dict = {}
        self.verdict_ready = False
        self.child_report = None
        self.rows: list[str] = []
        self._last_rev = None
        self._n_at_rev: dict = {}
        self._stop = threading.Event()
        self._live = None
        self.error = None

    def poll_once(self) -> None:
        try:
            self.item = _control(self.run, self.run.stage)
            counts = _arm_counts(self.run, self.run.stage, self.rid)
            changed = self.item.get("revision") != self._last_rev
            for arm, c in counts.items():                 # "new" = since the controller's last revision
                c["new"] = c["n"] - self._n_at_rev.get(arm, 0)
            self.counts = counts
            if changed:
                self._last_rev = self.item.get("revision")
                self._n_at_rev = {arm: c["n"] for arm, c in counts.items()}
                self.rows.append(DSP.footer_line(t0=self.t0, now=time.time(), item=self.item, counts=counts,
                                                 verdict_ready=False))
                if self.item.get("state") == "ROLLED_BACK" and "rolledBackAt" not in self.run.t:
                    at = time.time()
                    self.run.t["rolledBackAt"] = DSP.now_iso()
                    self.run.t["rolledBackAfterSeconds"] = round(at - self.t0, 1)
        except Exception as exc:  # noqa: BLE001 - the footer shows it and keeps polling
            self.error = f"{type(exc).__name__}"
        report = race.check_traffic_child(self.child, self.rid, self.t0, clock=time.time)
        if report and not self.child_report:
            self.child_report = report
            self.run.event("traffic child exited without finishedAt", code=report.code, killedAt=report.killed_at)

    def line(self) -> str:
        return DSP.footer_line(t0=self.t0, now=time.time(), item=self.item, counts=self.counts,
                               verdict_ready=self.verdict_ready,
                               child_note=self.child_report.note if self.child_report else self.error)

    def _loop(self) -> None:
        while not self._stop.wait(5):
            self.poll_once()

    def start(self) -> None:
        self.poll_once()
        threading.Thread(target=self._loop, daemon=True).start()
        if not self.run.args.no_live_footer:
            try:
                from rich.live import Live
                from rich.text import Text
                audience = bool(getattr(self.run, "explain", False))
                self._live = Live(get_renderable=lambda: Text(EX.audience_footer(self.line()) if audience
                                                              else self.line(), style="bold"),
                                  refresh_per_second=1, transient=False)
                self._live.start()
                self.run.screen._console = self._live.console
            except Exception:  # noqa: BLE001 - degraded mode
                self._live = None

    def degraded_rows(self) -> None:
        if self._live is None:
            for row in self.rows[-3:]:
                self.run.screen.say(row, "dim")
            self.run.screen.say(self.line(), "bold")

    @property
    def state(self) -> str | None:
        return self.item.get("state")

    @property
    def live_active(self) -> bool:
        """True while rich Live redraws the footer (explain mode prints nothing then)."""
        return self._live is not None and not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()
        if self._live is not None:
            self._live.stop()


def _start_traffic(run: Run, rid: str):
    log = LOCAL / f"traffic-{rid}.log"
    handle = open(log, "a", encoding="utf-8")  # noqa: SIM115 - the child owns it for its lifetime
    # nosemgrep: dangerous-subprocess-use-audit - list args, no shell; this repo's module11 CLI: fixed traffic shape, --canary-stage choice, armed rollout ID
    child = subprocess.Popen([PY, "-m", "module11", "traffic", "--stage", run.stage, *TRAFFIC, "--rollout-id", rid],
                             cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    run.screen.say(f"traffic driver started (pid {child.pid}, its own process group; log {log.relative_to(ROOT)})")
    run.screen.say(DSP.SYNTHETIC, "dim")
    return child


def _stop_child(child) -> None:
    if child is not None and child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def beat5(run: Run) -> None:
    s = {x.id: x for x in SUBBEATS[5]}
    armed = _armed(run)
    rid = armed.get("rolloutId")
    state: dict = {}

    def a() -> None:
        run.cmd("rollout", "show", "--stage", run.stage)
        item = _control(run, run.stage)
        n = sum(c["n"] for c in _arm_counts(run, run.stage, rid).values()) if rid else -1
        if not rid or item.get("rolloutId") != rid or item.get("state") not in ("WARMING", "OBSERVING") or n != 0:
            raise StopTake(3, f"not armed: {('demo-state.' + ('armed' if run.stage == 'prod' else 'devArmed'))} "
                              f"{rid}, control {item.get('state')} {item.get('rolloutId')}, {n} outcome item(s). "
                              "Ladder step 1: stop the take, run `module11 demo rearm`"
                              + (" (dev: `module11 demo arm --stage dev --rearm`)" if run.stage == "dev" else "")
                              + ", re-take `d 5`. No traffic was started.")
        if armed.get("rearmedBy"):
            run.screen.say(f"opened at {armed.get('openedAt')} by `demo rearm` (re-take of the approved release; "
                           f"audit record {armed.get('auditRecord')})", "yellow")
        else:
            run.screen.say(f"opened at {armed.get('openedAt')} by "
                           + ("the Production stage after the GitLab approval" if run.stage == "prod"
                              else "`module11 demo arm --stage dev` (dry-run rehearsal on dev)"))
        state["item"] = item
    run.step(s["5a"], a)
    run.explain_race_preview()
    run.key("Enter = t0: start the traffic > ", "")
    t0 = time.time()
    run.t["t0"] = DSP.now_iso()
    run.event("t0", rolloutId=rid)
    child = _start_traffic(run, rid)
    footer = Footer(run, rid, t0, child)
    run.notes["child"], run.notes["footer"] = child, footer
    footer.start()
    execution = (S.read().get("armed") or {}).get("pipelineExecutionId")
    if run.stage == "dev" or not execution:          # c.7: dev renders the prod records that exist, read-only
        try:
            execs = run.ctx.admin.client("codepipeline").list_pipeline_executions(
                pipelineName="m11-release", maxResults=1)["pipelineExecutionSummaries"]
            execution = execs[0]["pipelineExecutionId"] if execs else None
        except Exception:  # noqa: BLE001
            execution = None

    def f1() -> None:
        from module11.demo import rearm as R
        footer.degraded_rows()
        rows = _production_rows(run, execution)
        for stage in ("Source", "Analyze", "Image", "Seal", "Staging", "Approval", "ExtendedApproval", "Production"):
            latest = R._latest(rows, stage)
            text = ", ".join(f"{a} {r.get('status')}" for a, r in sorted(latest.items())) or "SKIPPED"
            run.screen.say(f"  {stage:<17} {text}")
        gate = R._latest(rows, "Staging").get("Gate") or {}
        summary = ((gate.get("output") or {}).get("executionResult") or {}).get("externalExecutionSummary")
        if summary:
            run.screen.say(f"  staging gate: {summary[:200]}")
        _label_execution(run, execution)

    def f2() -> None:
        from module11.demo import live_check
        footer.degraded_rows()
        try:
            gl = live_check._gitlab_client()
            tag = (S.read().get("armed") or {}).get("tag") or ""
            deps = gl.deployments("production")[:1]
            for d in deps:
                run.screen.say(f"  production: {d.get('ref')} {d.get('status')} by "
                               f"{(d.get('user') or {}).get('username')} at {d.get('created_at')}")
            t = gl.tag(tag) if tag else None
            if t:
                run.screen.say(f"  tag {t['name']}: {str(t.get('message') or '').splitlines()[:2]}")
                from module11.aws import gitlab_api as G
                run.screen.say(f"  package devops-companion-harness@{G.tag_number(tag)}: "
                               f"{', '.join(gl.package_files(G.tag_number(tag)))}")
            run.screen.ran_before(f"GitLab {tag}", str((deps[0] if deps else {}).get("created_at")))
        except Exception as exc:  # noqa: BLE001
            run.screen.say(f"  GitLab read failed ({type(exc).__name__}); the tabs show the same records", "yellow")

    def f4() -> None:
        footer.degraded_rows()
        run.cmd("shadow", "report", "model-migration")
        run.cmd("model", "compare", "show")
        run.cmd("abtest", "show")

    run.step(s["F1"], f1)
    run.step(s["F2"], f2)
    run.step(s["F3"], lambda: (footer.degraded_rows(), run.cmd("versions", "--stage", run.stage)))
    run.step(s["F4"], f4)
    run.step(s["F5"], lambda: (footer.degraded_rows(),
                               run.cmd("flags", "kill", "module10-recovery-planning", "--team", "payments",
                                       "--stage", run.stage)))
    footer.verdict_ready = True

    def f6() -> None:
        shown, ticked = 0, 0
        while True:
            for row in footer.rows[shown:]:
                run.screen.say(row)
            shown = len(footer.rows)
            if footer.child_report:
                run.screen.say(footer.child_report.note, "red")
                run.screen.say(footer.child_report.prompt, "bold red")
                raise StopTake(4, "traffic driver exited without finishedAt")
            if footer.state == "ROLLED_BACK":
                run.screen.say("ROLLED_BACK: press v for the verdict", "bold green")
                run.key("v = verdict > ", "v")
                return
            elapsed = time.time() - t0
            if elapsed >= STOP_S or footer.state == "PROMOTED":
                raise StopTake(4, f"no ROLLED_BACK at t0+{DSP.clock_text(elapsed)} (or PROMOTED): ladder step 4: "
                                  "stop the take, run `module11 demo rearm`, re-take `d 5`")
            if elapsed >= T_PROMPT_S and ticked < 2:
                if run.args.no_pause or run.key("t0+4:30: t ticks the controller now > ", "t") == "t":
                    run.cmd("rollout", "tick", "--stage", run.stage)
                    run.event("tick", n=ticked + 1)
                    ticked += 1
                    time.sleep(20)
            time.sleep(1)

    try:
        run.step(s["F6"], f6)
        run.step(s["5b"], lambda: (run.cmd("rollout", "show", "--stage", run.stage), run.screen.say(bake_line(run))))

        def c() -> None:
            release = footer.item.get("release") or "?"
            run.cmd("side-effects", "--stage", run.stage, "--release", release)
            run.cmd("memory", "purge", "--stage", run.stage, "--release", release, "--dry-run")
            report = race.check_traffic_child(child, rid, t0, clock=time.time)
            if report:
                run.screen.say(report.note, "red")
                raise StopTake(4, report.prompt)
            run.cmd("memory", "purge", "--stage", run.stage, "--release", release)
            _remember_canary_session(run, rid)
        run.step(s["5c"], c)
        run.step(s["5d"], lambda: run.screen.say("Rollback restores configuration, not consequences.", "bold yellow"))
    finally:
        footer.stop()
        run.screen._console = DSP._console()


def _remember_canary_session(run: Run, rid: str) -> None:
    """For 6a: one candidate payments session of this rollout that skipped search_runbooks."""
    from module11.aws import control_table
    from module11.release import naming
    rows = control_table.outcomes(run.ctx.admin.client("dynamodb"), naming.table(run.stage, "outcomes"), rid)
    pick = next((r for r in rows if r.get("arm") == "candidate" and r.get("team") == "payments"
                 and "search_runbooks" not in (r.get("toolsExecuted") or [])), None)
    if pick:
        # `scenario add --from-session` wants the runtime session ID (outcome field runtimeSessionId). The row's
        # `sk` is "<epoch ms>#<traffic id>", which the command rejects.
        S.update(lambda d: {**d, "lastCanary": {"rolloutId": rid, "stage": run.stage,
                                                "sessionId": pick.get("runtimeSessionId") or pick.get("sessionId")
                                                or pick.get("sk")}})


def _latest_completed_recommendation(directory: Path | None = None) -> str | None:
    """6e's fallback when demo-state holds no recommendation: the id of the newest COMPLETED record in
    module11/.local/recommendations (`module11 recommendation show` with no id picks the newest of any status; a
    failed or still-running newer record must not be shown or imported here). None when there is none."""
    found: list[tuple[str, str]] = []
    for path in (directory or LOCAL / "recommendations").glob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("status") == "COMPLETED":
            found.append((str(doc.get("createdAt") or ""), str(doc.get("id") or path.stem)))
    return max(found)[1] if found else None


# ---------------------------------------------------------------- B6
def beat6(run: Run) -> None:
    s = {x.id: x for x in SUBBEATS[6]}
    line, timing = race.revert_line(clock=time.time, read_version=lambda: _staging_version(run))
    if line:                               # tagged C6, so the 12-minute edit drops it with 2d-2e
        run.screen.sub = "C6"
        run.screen.say(line, "yellow")
        run.t["C6"] = timing

    def a() -> None:
        run.screen.say(bake_line(run))
        canary = S.read().get("lastCanary") or {}
        if not canary.get("sessionId"):
            run.screen.say("no canary session recorded (B5 sets it after the purge)", "yellow")
            return
        run.cmd("scenario", "add", "--from-session", canary["sessionId"], "--category", "regressions",
                "--stage", canary.get("stage", "prod"))
    run.step(s["6a"], a)
    run.step(s["6b"], lambda: (run.cmd("drift", "check", "--stage", "prod", "--external-only"),
                               run.screen.say("docs/MODEL_MIGRATION.md: the model-deprecation playbook", "dim")))
    run.step(s["6c"], lambda: run.cmd("cost", "--stage", "prod", "--by", "manifest"))
    run.step(s["6d"], lambda: run.cmd("corpus", "publish", "--corpus", "policy", "--change", "runbook-checkout-v2"))

    def e() -> None:
        rec = S.read().get("recommendation") or {}
        rid = (rec.get("id") if isinstance(rec, dict) else rec) or _latest_completed_recommendation()
        if not rid:
            run.screen.say("no recommendation recorded yet (the prep chain's recommendation step)", "yellow")
            return
        run.cmd("recommendation", "show", rid)
        run.cmd("recommendation", "import", rid)
    run.step(s["6e"], e)


def _staging_version(run: Run) -> str:
    from module11.aws import params
    current = params.stage_get(run.ctx.admin.client("ssm"), "staging", "current") or {}
    return f"v{(current.get('deployment') or {}).get('staging', {}).get('endpoints', {}).get('staging', '?')}"


# ---------------------------------------------------------------- B7
def beat7(run: Run) -> None:
    def a() -> None:
        for line in ("A completed DevOps agent:", "  every behavior input pinned in one manifest,",
                     "  every change through one gated path,",
                     "  every session and side effect naming its manifest version,",
                     "  and a bad release rolled back with one call.",
                     "Next: Module 12, securing this pipeline and auditing agent-initiated actions."):
            run.screen.say(line, "bold")
    run.step(SUBBEATS[7][0], a)


SECTIONS: dict[int, tuple[str, Callable[[Run], None]]] = {
    0: (TITLES[0], beat0), 1: (TITLES[1], beat1), 2: (TITLES[2], beat2), 3: (TITLES[3], beat3),
    4: (TITLES[4], beat4), 5: (TITLES[5], beat5), 6: (TITLES[6], beat6), 7: (TITLES[7], beat7),
}


# ---------------------------------------------------------------- replay
def render_capture(doc: dict, cut: str, *, say: Callable[[str, str | None], None],
                   on_sub: Callable[[str | None], None] | None = None) -> int:
    """Render a capture under the persistent banner, dropping events of sub-beats the cut level skips.
    `on_sub(sub)` runs after the banner each time a kept sub-beat starts (explain mode's panels)."""
    gone = skipped(cut)
    banner = DSP.replay_banner(doc.get("runIds") or [], doc.get("recordedAt", "?"))
    say(banner, "bold black on yellow")
    shown, current = 0, None
    for ev in doc.get("events") or []:
        if ev.get("kind") in ("header", "banner") or (ev.get("sub") is None and not ev.get("text")):
            continue                              # the replay prints its own heading and banner
        if ev.get("sub") in gone:
            if ev.get("sub") != current:
                say(f"[{ev['sub']}] skipped: cut level {cut}", "dim")
                current = ev.get("sub")
            continue
        if ev.get("sub") != current:
            current = ev.get("sub")
            say(banner, "bold black on yellow")
            if on_sub is not None:
                on_sub(current)
        say(ev.get("text", ""), ev.get("style"))
        shown += 1
    return shown


def replay_section(n: int, name: str, args, directory: Path | None) -> int:
    doc = RP.load(name, directory)
    screen = DSP.Screen()
    # Audience-clean labels: with explain on, and on the in-take `r` fallback (panels off, labels still clean).
    audience = bool(getattr(args, "explain", False) or getattr(args, "audience", False))
    if audience:
        screen.clean = functools.partial(EX.audience_line, replay=True)
    screen.say("")
    screen.say(f"{TITLES[n]}   (budget {DSP.clock_text(budget(n, args.cut))} at cut level {args.cut})", "bold cyan")
    if doc is None:
        where = "tests/fixtures/module11/captures" if directory else "module11/.local/captures"
        screen.say(f"no capture {name}.json in {where} yet: captures come only from real runs "
                   "(record with --record, then export with `module11 captures export`)", "yellow")
        return 0
    gone = skipped(args.cut)
    active = [s.id for s in SUBBEATS[n] if s.id not in gone]
    explain = bool(getattr(args, "explain", False)) and bool(active)
    on_sub = None
    if explain:
        screen.show(EX.beat_header(n, TITLES[n]))
    if audience:
        ids, seen = {s.id for s in SUBBEATS[n]}, set()

        def on_sub(sub: str | None) -> None:
            screen.sub = sub                             # the audience filter reads the sub-beat (B0's status)
            if explain and sub in ids and sub not in seen:   # unknown ids (B6's C6 line) get nothing
                seen.add(sub)
                block = EX.sub_block(sub, active)
                if block is not None:
                    screen.show(block)
    render_capture(doc, args.cut, say=lambda text, style: screen.say(text, style), on_sub=on_sub)
    if audience and n == 0 and "0a" in active:
        view = EX.status_view([e.get("text", "") for e in doc.get("events") or []
                               if e.get("sub") == "0a" and e.get("kind") == "out"])
        if view is not None:
            screen.show(view)
    if explain:
        screen.show(EX.takeaway(n, args.cut))
    return 0


# ---------------------------------------------------------------- main
def _timings(args, sections: list[int]) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{'all' if len(sections) > 1 else 's' + str(sections[0])}"
    return {"run": name, "sections": sections, "cut": args.cut, "pace": args.pace, "canaryStage": args.canary_stage,
            "record": args.record, "startedAt": DSP.now_iso(), "subBeats": [], "cutPoints": [], "events": [],
            "budgetSeconds": sum(budget(n, args.cut) for n in sections)}


def write_timings(t: dict) -> Path:
    t["endedAt"] = DSP.now_iso()
    started = datetime.fromisoformat(t["startedAt"])
    t["totalSeconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    out = Path(os.environ["M11_TIMINGS_DIR"]) if os.environ.get("M11_TIMINGS_DIR") else LOCAL / "timings"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{t['run']}.json"
    path.write_text(json.dumps(t, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def run_section(n: int, args, t: dict) -> int:
    screen = DSP.Screen(record=args.record)
    run = Run(args, n, screen, t)                # with explain on, sets the screen's audience filter first
    screen.header(f"{TITLES[n]}   (budget {DSP.clock_text(budget(n, args.cut))} at cut level {args.cut})")
    explain = run.explain and bool(run._active_ids())
    if explain:
        screen.show(EX.beat_header(n, TITLES[n]))
    started = time.time()
    code = 0
    try:
        SECTIONS[n][1](run)
    except Replay:
        _stop_child(run.notes.get("child"))
        run.event("replay", section=n)
        replay_args = args
        if run.explain:       # the in-take fallback replays an older capture: no panels claiming today's state,
            replay_args = argparse.Namespace(**{**vars(args), "explain": False, "audience": True})  # clean labels
        replay_section(n, RP.capture_name(n, n == 5), replay_args, None)
        return 0
    except StopTake as exc:
        _stop_child(run.notes.get("child"))
        screen.say(str(exc), "bold red")
        run.event("take stopped", code=exc.code, reason=str(exc))
        code = exc.code
    except KeyboardInterrupt:
        _stop_child(run.notes.get("child"))
        run.event("interrupted")
        code = 130
    else:
        if explain:                   # B5: after footer.stop(), so the deferred concepts print below the race
            for key in run.explain_deferred:
                screen.show(EX.concept_box(key))
            screen.show(EX.takeaway(n, args.cut))
    t.setdefault("sectionSeconds", {})[str(n)] = round(time.time() - started, 1)
    if args.record and code == 0:
        armed = _armed(run) if n == 5 else {}
        ids = [x for x in (t.get("run"), armed.get("rolloutId")) if x]
        doc = {"capture": f"b{n}", "section": n, "recordedAt": DSP.now_iso(), "runIds": ids, "cut": args.cut,
               "canaryStage": args.canary_stage, "events": screen.events, "responses": screen.responses}
        path = RP.save(doc)
        screen.say(f"capture written: {path.relative_to(ROOT)}", "dim")
        if n == 5:                               # B5's canary capture is the r fallback of c.5
            RP.save({**doc, "capture": "b5-canary"})
    return code


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Module 11 demo v2 (B0-B7)")
    p.add_argument("--section", type=int, choices=sorted(SECTIONS), help="one beat (d N); omit to run all")
    p.add_argument("--cut", choices=["full", "15", "12"], default=os.environ.get("M11_CUT") or "full",
                   help="pre-skip the cut list: 15 skips C1-C5, 12 skips C1-C8 (default full, or M11_CUT)")
    p.add_argument("--no-pause", action="store_true", help="never wait for a key")
    p.add_argument("--pace", choices=["none", "budget"], default="none",
                   help="budget: wait out each sub-beat's budget, so a dry run reproduces the clock")
    p.add_argument("--canary-stage", choices=["prod", "dev"], default="prod",
                   help="dev: run B5 against dev's demo-arm rollout (dry runs)")
    p.add_argument("--no-live-footer", action="store_true", help="degraded B5 footer: rows before each fill item")
    p.add_argument("--record", action="store_true", help="write module11/.local/captures/<beat>.json")
    p.add_argument("--replay", metavar="CAPTURE", help="render a local capture (b0-b7, b5-canary or a path)")
    p.add_argument("--replay-fixtures", action="store_true",
                   help="render the committed, redacted captures in tests/fixtures/module11/captures (no AWS)")
    p.add_argument("--notes", action="store_true", help="print the presenter's line under each sub-beat")
    p.add_argument("--explain", action="store_true", default=EX.env_enabled(),
                   help="audience panels, step cues and concept boxes (Module 10 style); or M11_EXPLAIN=1")
    return p


def section_of(capture: str) -> int:
    stem = Path(capture).stem
    digits = stem[1:2] if stem.startswith("b") else ""
    return int(digits) if digits.isdigit() and int(digits) in SECTIONS else 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cut not in CUT_LEVELS:
        parser().error(f"M11_CUT must be full, 15 or 12, not {args.cut!r}")
    sections = [args.section] if args.section is not None else sorted(SECTIONS)
    if not args.explain:                         # explain mode keeps presenter-only lines off the audience's screen
        print(f"cut level: {args.cut} (skips {', '.join(CUT_LEVELS[args.cut]) or 'nothing'}; "
              f"budget {DSP.clock_text(sum(budget(n, args.cut) for n in sections))})", flush=True)
    if args.replay:
        return replay_section(section_of(args.replay), args.replay, args, None)
    if args.replay_fixtures:
        t = _timings(args, sections)
        for n in sections:
            name = f"b{n}"
            if n == 5 and RP.load(name, RP.FIXTURES) is None:
                name = "b5-canary"
            replay_section(n, name, args, RP.FIXTURES)
            t["subBeats"] += [{"id": s.id, "section": n, "cut": s.cut, "skipped": s.id in skipped(args.cut),
                               "replay": True} for s in SUBBEATS[n]]
        t["replayFixtures"] = True
        write_timings(t)
        return 0
    t = _timings(args, sections)
    code = 0
    try:
        for n in sections:
            code = run_section(n, args, t) or code
            if code and len(sections) > 1 and n == 5:
                break
    finally:
        path = write_timings(t)
        if not args.explain:                     # still written; the path is presenter detail
            print(f"timings: {path.relative_to(ROOT)} (total {DSP.clock_text(t['totalSeconds'])}"
                  + (f", t0 -> ROLLED_BACK {DSP.clock_text(t['rolledBackAfterSeconds'])}"
                     if t.get("rolledBackAfterSeconds") is not None else "") + ")")
    return code


if __name__ == "__main__":
    sys.exit(main())
