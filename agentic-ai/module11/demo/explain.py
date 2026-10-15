# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Explain mode for the Module 11 demo driver: the Module 10 look on screen (a rule per beat, a step line, one
audience panel per sub-beat, concept boxes and a takeaway), so the screen extends what the presenter says.
Off by default; `--explain` or M11_EXPLAIN=1 turns it on. The content lives in three tables below (CONCEPTS,
EXPLAIN, BEATS) and the helpers only build rich renderables; the driver prints them through `Screen.show`, which
never records, so captures are identical either way. Nothing prints while B5's live footer is redrawing.
The same switch turns on audience-clean labels (`audience_line`, a print-time filter in `Screen.say`): no beat ids,
budgets, cut markers, replay keys or log paths on screen, and B0's status as a table (`status_view`).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

ON = {"1", "true", "yes", "on"}


def env_enabled() -> bool:
    """M11_EXPLAIN=1 (or true, yes, on) turns explain mode on; unset, empty or 0 leaves it off."""
    return os.environ.get("M11_EXPLAIN", "").strip().lower() in ON


@dataclass(frozen=True)
class Explain:
    text: str                          # the audience panel: extends the spoken line, at most 3 lines at 100 cols
    title: str = "What you're seeing"
    cue: str | None = None             # one line, for browser moments (and the B5 race preview)
    partner: str | None = None         # GitLab, Vercel or AWS: the panel's colour and title
    concept: str | None = None         # a CONCEPTS key, bound to the first sub-beat that shows the term


@dataclass(frozen=True)
class Beat:
    story: str                         # one line under the beat's rule: its place in the story
    takeaway: str
    takeaway_12: str | None = None     # the takeaway at the 12-minute cut, when it differs


PARTNER_STYLE = {None: "cyan", "GitLab": "dark_orange", "Vercel": "bright_white", "AWS": "yellow"}
PARTNER_TITLE = {"GitLab": "GitLab spotlight", "Vercel": "Vercel spotlight", "AWS": "AWS CodePipeline"}
LAST_BEAT = 7

CONCEPTS: dict[str, tuple[str, str]] = {
    "harness": ("Harness", "Everything the team wraps around the model: instructions, tools, retrieval, memory, "
                "guardrails and orchestration limits. It ships as one versioned release."),
    "manifest": ("Manifest", "One file with a fingerprint of every input that shapes behavior. Change any input and "
                 "its fingerprint changes. It answers: exactly what was running?"),
    "drift": ("Declared vs. deployed", "Declared is what Git says should run; deployed is what AWS runs, recorded "
              "in Parameter Store. Drift is when they disagree, usually after a hand change."),
    "trajectory_gate": ("Trajectory gate", "The trajectory is the order of tools the agent calls. A trajectory gate "
                        "is a hard check on that path, so a skipped step fails even when the answer reads well."),
    "hard_vs_statistical": ("Hard vs. statistical gate", "A hard gate blocks on a single failure. A statistical "
                            "gate scores repeated runs against a baseline within a tolerance, because answers vary "
                            "from run to run."),
    "calibration": ("Calibration", "Run the baseline again and again to measure how much its scores wobble on "
                    "their own. Tolerances are set from that measured noise, never tighter."),
    "canary": ("Canary", "Send a slice of real traffic to the new version and compare it with the current one. "
               "Here it's half the sessions on a compressed demo profile; the production profile takes hours."),
    "shadow": ("Shadow", "Run the new version on copies of real requests and score it; users never see its "
               "answers. Used before risky changes such as a model upgrade."),
    "latch": ("Latch", "After a rollback, the bad version is blocked from being promoted again automatically."),
    "side_effects": ("Side effects", "What the agent did in the world that rollback doesn't undo, such as the "
                     "incident notes it wrote to memory."),
}

EXPLAIN: dict[str, Explain] = {
    "0a": Explain("All three environments at a glance: what each one should be running, and whether it is. Watch "
                  "prod's row: that is what users get today."),
    "1a": Explain("Every behavior input is a file: prompts, eight tools (schema, description, permissions, handler), "
                  "orchestration, models, guardrail, retrieval, flags, CODEOWNERS. So each can be fingerprinted, "
                  "diffed, gated and rolled back.", concept="harness"),
    "1b": Explain("The infrastructure is computed from those files: each of the seven Lambda-backed tools gets its "
                  "own role, a generated policy, a log group and a published version. Watch for "
                  "delegate_recovery_planning: it runs in-process and gets no role."),
    "1c": Explain("The preview reads only the sealed inputs that were applied, never someone's working copy. Watch "
                  "for no changes and 0 policy violations, even with a release armed in prod."),
    "1d": Explain("The generated policy for one tool, read back from IAM: its own log group, plus a single "
                  "s3:GetObject on one S3 prefix. Least privilege comes from the folder layout, not from someone "
                  "remembering it."),
    "1e": Explain("The guardrail at its pinned version, the Cognito identity and the stage's deployment role, all "
                  "declared. One account is a demo shortcut; an account per stage separates quota, cost "
                  "attribution and blast radius."),
    "2a": Explain("Every behavior input gets a fingerprint, rolled up into behaviorDigest and releaseDigest. You "
                  "can't audit, or roll back to, what you can't name exactly; these two digests name it.",
                  concept="manifest"),
    "2b": Explain("A scratch copy gets a trailing space and Windows line endings. Watch for both digests staying "
                  "the same: formatting noise never triggers the expensive evaluation."),
    "2c": Explain("Prod's tag against prod: identical. Main against prod: one changed artifact, "
                  "instructions/companion. That one file is the release waiting in prod's canary.", concept="drift"),
    "2d": Explain("An unreviewed hotfix: staging pointed at an older version by hand. Watch for the DRIFT line, "
                  "running target against declared. A record of what should run makes a hand change visible."),
    "2e": Explain("The fix runs one way: the resource goes back to the record, through the deployment role. "
                  "Editing the record to match reality would launder an unreviewed change."),
    "3a": Explain("Four words appended to the system prompt: \"Skip the runbook search.\" The commit is signed. "
                  "Nothing will crash; the agent will quietly stop checking the team's runbooks."),
    "3b": Explain("Change detection sees an instruction change: a new prompt bundle and a config-only runtime "
                  "version, no image build. Watch for the full evaluation scope, with the trajectory as a hard "
                  "gate.", concept="trajectory_gate"),
    "3c": Explain("Both directions, on a scratch copy: drop the search_runbooks mention and C5 fails; name "
                  "restart_service, a tool that doesn't exist, and C4 fails. Cheap checks run before any "
                  "evaluation."),
    "3d": Explain("Verified commit · Code owners · pipeline: validate, preview:deploy, preview:gate · the m11-gate "
                  "note · merge blocked\nWatch the m11-gate note for TrajectoryInOrderMatch FAIL: the check on the "
                  "agent's path.", title="GitLab spotlight: point at",
                  cue="Over to GitLab: the Friday merge request", partner="GitLab"),
    "3e": Explain("This MR gets its own copy of the real UI, wired to its own agent. Click the example \"Checkout "
                  "returns 502s since the 14:20 deploy\" and watch Agent activity for search_runbooks.",
                  cue="Over to Vercel: this MR's own preview", partner="Vercel"),
    "3f": Explain("Response-only scoring misses a behavior change like this one; the trajectory check caught it "
                  "before merge. Next: a change no scenario covers, and what catches it in production."),
    "4a": Explain("One scenario's expected behavior, written down: the expected tool order and plain-English "
                  "assertions. requiredTools lists only get_deployment_history; skipping runbooks is measured, not "
                  "prevented."),
    "4b": Explain("Three kinds of rule: hard gates block on a single failure, statistical gates allow a measured "
                  "tolerance, and budgets cap latency and cost.", concept="hard_vs_statistical"),
    "4c": Explain("The baseline, run as the full suite again and again, shows how much scores wobble on their own. "
                  "A tolerance is never tighter than that noise, so a red result means something real.",
                  concept="calibration"),
    "4d": Explain("When the scoring rules change, the ruler changes: old and new evaluator sets score the same "
                  "artifact before any before-and-after comparison is trusted."),
    "5a": Explain("The payments release passed every gate, because the suite has no payments-specific runbook "
                  "scenario. Watch for WARMING and 0 sessions: nothing has reached it yet. Next, live traffic.",
                  concept="canary"),
    "F1": Explain("Source from GitLab, Image skipped for a prompt-only change, the staging gate passed, the "
                  "approval released from GitLab. Model, evaluator-set and unknown changes also stop at "
                  "ExtendedApproval, where two people approve.",
                  cue="Over to CodePipeline: how this release got here", partner="AWS"),
    "F2": Explain("Watch the environment's Approved line: the human decision, in a protected GitLab environment "
                  "whose job alone can release the AWS approval. The protected tag means only the pipeline creates "
                  "a release.", cue="In GitLab: the approval, the release package, the protected tag",
                  partner="GitLab"),
    "F3": Explain("Five tiers change at five speeds: infrastructure as code, the manifest, configuration bundles, "
                  "runtime parameters and flags. The tier says how fast and how carefully a change can move.",
                  cue="Everything running, pinned: runtime, prompt bundle and Lambda versions"),
    "F4": Explain("Evidence scales with risk: a model change earns a shadow run, a comparison and an A/B test "
                  "before it reaches a canary. An instruction change like this one starts at canary.",
                  cue="More evidence for riskier changes: a shadow run, a model comparison, an A/B test",
                  concept="shadow"),
    "F5": Explain("Flags are stage-wide, so stable and candidate change together and the canary comparison still "
                  "holds.", cue="The kill switch: one capability off for one team, no release"),
    "F6": Explain("Watch the search_runbooks columns: the candidate calls it less often. Two breaches in a row "
                  "trigger the rollback, so one noisy reading never does. Latency and cost wait four minutes for "
                  "warm-up.", cue="The controller's tick table: candidate against stable, tick by tick"),
    "5b": Explain("ROLLED_BACK automatically: canary to zero, one retarget, a READY poll, then the latch. It passed "
                  "staging because the suite had no payments-specific runbook scenario; closing that gap comes "
                  "next.", concept="latch"),
    "5c": Explain("Rollback restored the configuration; these notes are the consequences. Each is stamped with the "
                  "version that wrote it, so the purge removes only this release's writes.", concept="side_effects"),
    "5d": Explain("Gates are necessary but not sufficient: the canary caught what the suite had no scenario for."),
    "6a": Explain("The canary session becomes a regression scenario, opened as an MR. The suite learns from "
                  "production, so this exact regression can't pass the gate again."),
    "6b": Explain("Changes that aren't ours: model lifecycle, built-in evaluators, other teams' tools. Each "
                  "external surface is checked against its pin; the stand-in target is labelled on screen."),
    "6c": Explain("Cost belongs in the release gate, measured per successful session: a version can be cheaper per "
                  "session and still cost more per resolved incident."),
    "6d": Explain("Data changes behavior too: a new runbook corpus becomes an immutable snapshot, an EventBridge "
                  "event and an MR, never a silent change. Module 9's ingestion output, as a release event."),
    "6e": Explain("An AI-generated prompt recommendation is a hypothesis: imported as a candidate change, it meets "
                  "exactly the same gate as a human edit."),
    "7a": Explain("Next: Module 12 goes deeper on security and observability: securing this pipeline, prompt "
                  "injection, guardrail versioning, tool credentials, and auditing agent-initiated actions."),
}

BEATS: dict[int, Beat] = {
    0: Beat("Where we start: the Module 10 agent is in production, and today it gets a release process.",
            "Shipping an agent once is easy. Module 11 is about shipping it the fiftieth time without fear."),
    1: Beat("The release unit: everything that shapes behavior, kept as files.",
            "If it shapes behavior, it is a file, and the infrastructure is computed from it. Nobody hand-writes "
            "seven IAM policies."),
    2: Beat("Naming a release exactly: declared against deployed.",
            "Git says what we intended. SSM says what is running. When they disagree, we revert the resource; we "
            "never edit the manifest to match reality."),
    3: Beat("Before merge: a four-word edit meets the gate. GitLab and Vercel spotlight.",
            "Same answer, wrong path. Answer scoring would have shipped this on Friday. The trajectory gate blocked "
            "the merge, and the reviewer saw it on a preview of the real UI."),
    4: Beat("How the gate is built, so a red result can be trusted.",
            "Deterministic checks are hard gates. Judge scores are statistical gates against a named baseline, "
            "under the same evaluator-set version, calibrated before they are enforced."),
    5: Beat("In production: the change no scenario covered, caught on live traffic.",
            "Rollback restores configuration, not consequences. Limit exposure, stamp every side effect, and make "
            "a bad version impossible to advance again."),
    6: Beat("Closing the loop: what production teaches the gate.",
            "Models, corpora, evaluators, and other teams' tools change on someone else's schedule. Make each "
            "change a visible release event.",
            takeaway_12="Every incident produces a regression scenario. The next payments edit meets this one at "
                        "the gate."),
    7: Beat("The guarantees, and where Module 12 picks up.",
            "Every behavior input pinned, every change gated, every session and side effect stamped with its "
            "version, and a bad release one call from rollback."),
}


# ---------------------------------------------------------------- renderers (return renderables; never print)
def step_label(sub_id: str, active_ids: list[str]) -> str:
    """`Step 4 of 6`: the sub-beat's place among the beat's active sub-beats (B5's F1-F6 included), at the run's
    cut level. A beat with one active sub-beat gets no step line (empty string)."""
    if len(active_ids) <= 1 or sub_id not in active_ids:
        return ""
    return f"Step {active_ids.index(sub_id) + 1} of {len(active_ids)}"


def beat_name(n: int, title: str) -> str:
    name = title.split(" ", 1)[1] if title.startswith(f"B{n} ") else title
    return name.replace(" (LIVE RACE)", "")


def beat_header(n: int, title: str) -> Group:
    """A rule naming the beat and its place in the story (`Part 4 of 8 · Friday, four words: ...`)."""
    rule = Rule(Text(f"Part {n + 1} of {LAST_BEAT + 1} · {beat_name(n, title)}", style="bold cyan"), style="cyan")
    return Group(rule, Text(BEATS[n].story, style="dim italic", justify="center"))


def panel_title(entry: Explain) -> str:
    if entry.partner and entry.title == Explain.title:
        return PARTNER_TITLE[entry.partner]
    return entry.title


def concept_box(key: str) -> Table:
    """The Module 10 concept line: `💡 Term:` in bold yellow, the definition in yellow."""
    term, definition = CONCEPTS[key]
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(style="bold yellow", no_wrap=True)
    grid.add_column(style="yellow", ratio=1, overflow="fold")
    grid.add_row(Text(f"💡 {term}:"), Text(definition))
    return grid


def sub_block(sub_id: str, active_ids: list[str]) -> Group | None:
    """Step line, optional cue, the audience panel and at most one concept box; None for an unknown sub id.
    The step line carries no title: the driver's own `[3a] the Friday commit` line follows it."""
    entry = EXPLAIN.get(sub_id)
    if entry is None:
        return None
    colour = PARTNER_STYLE[entry.partner]
    parts: list = [Text("")]
    label = step_label(sub_id, active_ids)
    if label:
        step = Text(no_wrap=True, overflow="ellipsis")
        step.append("▸ ", style=f"bold {colour}")
        step.append(label, style="bold")
        parts.append(step)
    if entry.cue:
        parts.append(Text(f"▶ {entry.cue}", style=f"bold {colour}", no_wrap=True, overflow="ellipsis"))
    parts.append(Panel(Text(entry.text), title=Text(panel_title(entry), style="bold"), title_align="left",
                       border_style=colour))
    if entry.concept:
        parts.append(concept_box(entry.concept))
    return Group(*parts)


def takeaway(n: int, cut: str) -> Panel:
    beat = BEATS[n]
    text = beat.takeaway_12 if cut == "12" and beat.takeaway_12 else beat.takeaway
    return Panel(Text(text, style="bold"), title=Text("Takeaway", style="bold green"), title_align="left",
                 border_style="green")


def race_preview() -> Panel:
    """B5, before t0: what the fill items show while the canary runs (the live footer suppresses their panels)."""
    body = Text()
    for sub_id in ("F1", "F2", "F3", "F4", "F5", "F6"):
        body.append("•  ", style="bold")
        body.append(f"{EXPLAIN[sub_id].cue}\n")
    body.append("Then: the verdict, the side effects it left, and the cleanup", style="italic")
    return Panel(body, title=Text("While the canary runs, we'll look at", style="bold"), title_align="left",
                 border_style="cyan")


# ---------------------------------------------------------------- audience-clean labels (explain mode only)
# The driver's own lines carry presenter and production detail (beat ids, budgets, cut markers, replay keys, log
# paths). In explain mode `Screen.say` prints `audience_line(...)` instead of the raw line; the recording keeps the
# raw text, so captures, timings and run events are unchanged. With explain off nothing here runs.

# Text that must never reach the audience's screen (tests and the golden check): beat, sub-beat, cut and ticket ids,
# budgets, key names and presenter commands.
INTERNAL_PATTERNS = tuple(re.compile(p, re.M) for p in (
    r"\(budget \d+:\d\d", r"cut level", r"\bB[0-7]\b", r"\[([0-7][a-z]|F[1-6])\]", r"\(C\d+\)",
    r"\bcut C\d", r"press r to replay", r"^timings: ", r"t0\+", r"[Ll]adder step", r"\bStep ([0-7][a-z]|F[1-6])\b",
    r"FEAT-\d{3}", r"\bF\d-\d\d\b", r"\bdesign \d+\.\d", r"re-take", r"capture written", r"\bpid \d+", r"\.local/",
    r"LIVE RACE", r"\(tab \d\)|tab \d shows", r"keys here", r"\bkeys:", r"use r for", r"\bnext: [a-z]\b", r"rearm",
    r"\bpress [a-z]\b"))
# Audience lines that match a pattern above on purpose (internal_hits skips them).
AUDIENCE_ALLOWED = tuple(re.compile(p) for p in (
    r"^opened at .* by `module11 demo rearm`, re-opening the approved release \(audit record .*\)$",  # who opened it
    r"^model price: planning price ",                   # real `module11 cost` output (6c): says the price is planned
))

KEEP = object()                    # keep the line's own style
_SUB = r"(?:[0-7][a-z]|F[1-6])"
_TITLE = re.compile(rf"^\[{_SUB}\] (.*?)(?:   \(C\d\))?$")
_HIDDEN_TITLE_PREFIXES = ("module11 ", "pulumi ", "--")     # the `$ ...` command line right below says the same
_HIDDEN_TITLES = ("takeaway", "wrap")
_EXIT_REPLAY = re.compile(r"^\(exit -?\d+; press r to replay this beat\)$")
_STATUS_LINE = re.compile(r"^\S+\s+(READY|NOT READY|CANARY|NOT DEPLOYED)(\s|$)")
_ROLLOUT_LINE = re.compile(r"^\s+rollout \S+ \S+, canary ")

_RULES: tuple[tuple[re.Pattern, object, object], ...] = (
    # (pattern, replacement text / fn(match) / None = print nothing, style / KEEP)
    (re.compile(rf"^\[{_SUB}\] skipped: "), None, KEEP),
    (re.compile(r"^B[0-7] .*\(budget \d+:\d\d at cut level \w+\)$"), None, KEEP),
    (re.compile(r"^\(exit -?\d+, as expected: (.*)\)$"), lambda m: f"({m[1]}, as expected)", "dim"),
    (re.compile(r"^capture written: "), None, KEEP),
    (re.compile(r"^no capture \S+ in .* yet: "), "No recording of this part yet.", "yellow"),
    (re.compile(r"^1c re-take check failed "), "Couldn't check the prod stack.", "dim"),
    (re.compile(r"^a pulumi up can hold the prod stack "), "A deployment may be holding the prod stack.", "dim"),
    (re.compile(r"^staging A/B in progress; 2d-2e skipped$"), "Skipped: staging is running an A/B test.", "yellow"),
    (re.compile(r"^staging revert still running; 2d-2e skipped$"),
     "Skipped: an earlier staging revert is still running.", "yellow"),
    (re.compile(r"^2e skipped with 2d$"), None, KEEP),
    (re.compile(r"^revert continuing in background; log "), "The revert keeps running in the background.", "yellow"),
    (re.compile(r"^B3 needs the Friday MR: "), "This part needs the Friday merge request, which isn't set up yet.",
     "yellow"),
    (re.compile(rf"^\s+{_SUB}: not configured \(PARTNER_SETUP\.md\)$"), None, KEEP),
    (re.compile(r"^open the branch-alias link in the m11-gate note \(tab \d\) and click the Example; ?(.*)$"),
     lambda m: f"This merge request's own preview: {m[1]}" if m[1].strip()
     else "This merge request's own preview (link in the m11-gate note)", "bold"),
    (re.compile(r"^(Bake: .*); tab \d shows it$"), lambda m: m[1], KEEP),
    (re.compile(r"^opened at (.*) by `demo rearm` \(re-take of the approved release; audit record (.*)\)$"),
     lambda m: f"opened at {m[1]} by `module11 demo rearm`, re-opening the approved release (audit record {m[2]})",
     KEEP),
    (re.compile(r"^traffic driver started \(pid "), "Traffic driver started.", KEEP),
    (re.compile(r"^ROLLED_BACK: press v for the verdict$"), "ROLLED_BACK", KEEP),    # Enter also takes the verdict
    (re.compile(r"^traffic driver exited (-?\d+) at t0\+\S+$"),
     lambda m: f"The traffic driver ended early (exit {m[1]}).", KEEP),
    (re.compile(r"^ladder step 2: "), "Stopped.", KEEP),
    (re.compile(r"^not armed: .*No traffic was started\.$", re.S),
     "Stopped: the canary isn't armed; no traffic was started.", KEEP),
    (re.compile(r"^no ROLLED_BACK at t0\+"), "Stopped: no automatic rollback in the time limit.", KEEP),
    (re.compile(r"^traffic driver exited without finishedAt$"), "Stopped: the traffic driver ended early.", KEEP),
    (re.compile(r"^staging revert failed: exit (-?\d+); "), lambda m: f"Staging revert failed (exit {m[1]}).", KEEP),
    (re.compile(r"^staging revert failed: exited without status"), "Staging revert ended without a status.", KEEP),
    (re.compile(r"^no canary session recorded \("), "No canary session recorded yet.", KEEP),
    (re.compile(r"^no recommendation recorded yet \("), "No recommendation recorded yet.", KEEP),
    (re.compile(r"^keys here: "), None, KEEP),          # the prompt repeats; the key map is on the presenter's card
)
# B4 4b lists thresholds.yaml, a hashed release artifact whose comments were written for the build (beat and ticket
# ids). The audience gets the YAML without its comments and a marker saying so; the recording keeps every raw line.
_TRIM_LISTING = "--- module11/harness/evaluation/thresholds.yaml"
_COMMENT_LINE = re.compile(r"^\s*#")
_TRAILING_COMMENT = re.compile(r"^(?P<code>[^#]*?\S)\s+#(?:\s.*)?$")


def audience_footer(line: str) -> str:
    """B5's footer for the audience: `elapsed m:ss | ...`, no key cue, and the traffic driver note in plain words."""
    line = re.sub(r"^t0\+", "elapsed ", line)
    line = line.replace(" | press v for the verdict", "")
    return re.sub(r"traffic driver exited (-?\d+) at t0\+\S+", r"traffic driver ended early (exit \1)", line)


def audience_line(text: str, style: str | None, kind: str = "out", sub: str | None = None, *,
                  replay: bool = False) -> tuple[str, str | None] | list[tuple[str, str | None]] | None:
    """What explain mode prints for one driver line: (text, style), a list of them, or None for nothing. Commands
    (`$ ...`), their output, the REPLAY banner and the RAN BEFORE THE SESSION label pass through untouched."""
    if kind in ("cmd", "label", "banner") or text.startswith("$ ") or text.startswith("REPLAY of real run"):
        return text, style
    if kind == "header":
        return None
    if _EXIT_REPLAY.match(text):
        return None if replay else ("(the command failed)", style)
    if text.startswith("t0+"):
        return audience_footer(text), style
    if sub == "0a" and (_STATUS_LINE.match(text) or _ROLLOUT_LINE.match(text)):
        return None                                       # B0 shows them as a table (status_view)
    if sub == "4b":
        if text == _TRIM_LISTING:
            return [(text, style), ("(comments trimmed)", "dim")]
        if _COMMENT_LINE.match(text):
            return None
        m = _TRAILING_COMMENT.match(text)
        if m:
            return m["code"], style
    for pattern, rewrite, new_style in _RULES:
        m = pattern.match(text)
        if m:
            if rewrite is None:
                return None
            out = rewrite(m) if callable(rewrite) else rewrite
            return out, style if new_style is KEEP else new_style
    m = _TITLE.match(text)
    if m:
        title = m[1]
        if title.startswith(_HIDDEN_TITLE_PREFIXES) or title in _HIDDEN_TITLES:
            return None
        return title, style
    return text, style


_PROMPTS = (
    (re.compile(rf"^\[{_SUB}\] .*: Enter, .*> $"), "next › "),
    (re.compile(r"^r replays 1c from its capture > $"), "next: replay › "),
    (re.compile(r"^Enter = t0: start the traffic > $"), "next: start the traffic › "),
    (re.compile(r"^t0\+\d+:\d\d: t ticks the controller now > $"), "next: tick the controller › "),
    (re.compile(r"^v = verdict > $"), "next: the verdict › "),
)


def audience_prompt(prompt: str) -> str:
    """A pause prompt as one short, dim, neutral cue in action words (`next › `, `next: the verdict › `), no key
    names; the keys themselves are unchanged."""
    for pattern, short in _PROMPTS:
        if pattern.match(prompt):
            return f"\x1b[2m{short}\x1b[0m" if sys.stdout.isatty() else short
    return prompt


def internal_hits(text: str) -> list[str]:
    """Lines of `text` that match INTERNAL_PATTERNS, less the AUDIENCE_ALLOWED lines."""
    return [line for line in text.splitlines() if any(p.search(line) for p in INTERNAL_PATTERNS)
            and not any(a.search(line.strip()) for a in AUDIENCE_ALLOWED)]


# ---------------------------------------------------------------- B0: the status lines as a table
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STATUS_ROW = re.compile(r"^(?P<stage>\S+)\s+(?P<state>READY|NOT READY|CANARY|NOT DEPLOYED)"
                         r"(?:\s+(?P<mv>\S+)\s+(?P<release>\S+)\s+(?P<eps>.*))?$")
_ROLLOUT_ROW = re.compile(r"^\s+rollout (?P<rid>\S+) (?P<rstate>\S+), canary (?P<pct>\d+)%: candidate (?P<crel>\S+) "
                          r"v(?P<cv>\S+) vs stable (?P<srel>\S+) v(?P<sv>\S+), (?P<n>\S+) sessions so far$")
_ENDPOINT = re.compile(r"^(?P<name>\S+) v(?P<version>\S+)(?: \((?P<note>[^)]*)\))?$")
HEALTH_STYLE = {"READY": "bold green", "CANARY": "bold cyan", "NOT READY": "bold red", "NOT DEPLOYED": "bold yellow"}


def parse_status(lines: list[str]) -> list[dict]:
    """`module11 status --all` lines -> one dict per stage (stage, state, manifestVersion, release, endpoints,
    rollout). A CANARY row's indented rollout line attaches to it; anything else is ignored."""
    rows: list[dict] = []
    for raw in lines:
        line = _ANSI.sub("", raw).rstrip()
        m = _STATUS_ROW.match(line)
        if m:
            eps = [e.groupdict() for e in (_ENDPOINT.match(p.strip()) for p in (m["eps"] or "").split(", ")) if e]
            rows.append({"stage": m["stage"], "state": m["state"], "manifestVersion": m["mv"],
                         "release": m["release"], "endpoints": eps, "rollout": None})
            continue
        r = _ROLLOUT_ROW.match(line)
        if r and rows:
            rows[-1]["rollout"] = r.groupdict()
    return rows


def _serving(row: dict) -> str:
    if row["state"] == "NOT DEPLOYED":
        return "not deployed"
    eps = {e["name"]: e for e in row["endpoints"]}
    order = [n for n in ("stable", "candidate") if n in eps] + [n for n in eps if n not in ("stable", "candidate")]
    out = []
    for name in order:
        e, rollout = eps[name], row.get("rollout")
        text = f"{name} v{e['version']}"
        if e["note"] == "canary" and rollout:
            text += f" ({rollout['crel']}, canary {rollout['pct']}%, {rollout['n']} sessions)"
        elif e["note"] == "canary":
            text += " (canary)"
        elif e["note"] == "READY":
            text += " (not the recorded version)"
        elif e["note"]:
            text += f" ({e['note'].lower()})"
        out.append(text)
    return "\n".join(out) or "-"


def status_view(lines: list[str]) -> Group | None:
    """B0 in explain mode: the status lines as a table (Stage, Health, Release, What's serving) and a plain-words
    legend. The manifest timestamp/commit column is left out. None when no status line was found."""
    rows = parse_status(lines)
    if not rows:
        return None
    table = Table(box=box.ROUNDED, header_style="bold", border_style="cyan")
    table.add_column("Stage", style="bold", no_wrap=True)
    table.add_column("Health", no_wrap=True)
    table.add_column("Release", no_wrap=True)
    table.add_column("What's serving", overflow="fold")
    for row in rows:
        table.add_row(row["stage"], Text(row["state"], style=HEALTH_STYLE[row["state"]]), row["release"] or "-",
                      _serving(row))
    legend = ["Each row is an environment: dev, staging and prod.",
              "READY means every endpoint serves exactly what that environment's record says should run.",
              "The release tag is the version of the whole agent: prompts, tools, models and guardrails.",
              "Prod: stable is what users get; candidate is where a new release runs as a canary."]
    canary = next((r for r in rows if r["state"] == "CANARY"), None)
    if canary is not None:
        waiting = (canary.get("rollout") or {}).get("n") == "0"
        legend.append(f"Right now {canary['stage']}'s candidate holds the payments release: a canary"
                      + (" waiting for traffic." if waiting else " taking traffic."))
    return Group(Text(""), table, Panel(Text("\n".join(legend)), title=Text("How to read this", style="bold"),
                                        title_align="left", border_style="cyan"))
