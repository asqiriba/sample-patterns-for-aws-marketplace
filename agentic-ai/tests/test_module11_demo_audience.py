# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Audience-clean screen in explain mode (module11/demo/explain.py audience_line, Screen.clean): no beat ids,
budgets, cut markers, replay keys, log paths or ladder wording on screen in any beat, live or replay; commands,
their output, the REPLAY banner and RAN BEFORE THE SESSION stay verbatim; recordings and timings keep the raw
text; flag off is unchanged. B0's status lines render as a table with a legend. AWS reads are stubbed."""
from __future__ import annotations

import builtins
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console

from module11.demo import display as DSP
from module11.demo import explain as EX
from module11.demo import race
from test_module11_demo_explain import _LiveStub, _args, _console, _env, _screen, _subsequence, _timings, _verbatim
from test_module11_demo_smoke import driver, run_driver

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "module11/.local/audience-golden/before-off"
CUTS = ("full", "15", "12")
USER_LINES = [                       # what the presenter saw on 2026-10-13 (prod armed, before the CANARY rule)
    "dev      READY     2026-10-09T00:53:47Z/f34c577  harness-v19  candidate v24, stable v24",
    "staging  READY     2026-10-09T19:14:15Z/a701bd0  harness-v28  staging v14",
    "prod     NOT READY 2026-10-09T17:10:09Z/83ce7d4  harness-v27  candidate v14 (READY), stable v13"]
CANARY_LINES = USER_LINES[:2] + [
    "prod     CANARY    2026-10-09T17:10:09Z/83ce7d4  harness-v27  candidate v14 (canary), stable v13",
    "         rollout r-1 WARMING, canary 50%: candidate harness-v28 v14 vs stable harness-v27 v13, 0 sessions so far"]
FOOTER = DSP.footer_line(t0=100.0, now=142.0, item={"state": "ROLLED_BACK"}, counts={}, verdict_ready=True,
                         child_note="traffic driver exited 1 at t0+1:18")
# Every presenter/production string the driver can print (the plan's inventory), as (text, style, kind, sub).
RAW = [
    ("B1 The harness is the release unit, declared in code   (budget 2:00 at cut level full)", "bold cyan", "header",
     None),
    ("B5 The one that got through: canary and automatic rollback (LIVE RACE)   (budget 5:30 at cut level 15)",
     "bold cyan", "out", None),
    ("[0a] module11 status --all", "bold", "out", "0a"), ("[3c] consistency check, both directions   (C4)", "bold",
                                                           "out", "3c"),
    ("[F1] CodePipeline: the payments release", "bold", "out", "F1"), ("[3f] takeaway", "bold", "out", "3f"),
    ("[3c] skipped: cut C4 (level 15)", "dim", "out", None), ("[3c] skipped: cut level 15", "dim", "out", None),
    ("[3c] skipped: s", "dim", "out", None), ("keys here: Enter, s, r", "dim", "out", "1a"),
    ("(exit 1; press r to replay this beat)", "yellow", "out", "0a"),
    ("(exit 1, as expected: the check refuses the edit)", "dim", "out", "3c"),
    ("capture written: module11/.local/captures/b0.json", "dim", "out", "0a"),
    ("no capture b1.json in tests/fixtures/module11/captures yet: captures come only from real runs "
     "(record with --record, then export with `module11 captures export`)", "yellow", "out", None),
    ("1c re-take check failed (ClientError: denied); use r for 1c", "yellow", "out", "1c"),
    ("a pulumi up can hold the prod stack (Deploy InProgress, Bake Succeeded, rollout WARMING); use r for 1c",
     "yellow", "out", "1c"),
    ("staging A/B in progress; 2d-2e skipped", "yellow", "out", "2d"),
    ("staging revert still running; 2d-2e skipped", "yellow", "out", "2d"), ("2e skipped with 2d", "dim", "out", "2e"),
    ("revert continuing in background; log module11/.local/drift-revert-20261013T150000Z.log", "yellow", "out", "2e"),
    ("B3 needs the Friday MR: the GitLab MR flow and the Vercel preview are not configured yet (PARTNER_SETUP.md sets "
     "them up). This path renders at its 3:00 budget; the timing counts as budget, not measured.", "yellow", "out",
     None),
    ("  3a: not configured (PARTNER_SETUP.md)", "dim", "out", "3a"),
    ("open the branch-alias link in the m11-gate note (tab 2) and click the Example; https://m11-mr-7.example",
     "bold", "out", "3e"),
    ("open the branch-alias link in the m11-gate note (tab 2) and click the Example; ", "bold", "out", "3e"),
    ("Bake: Failed. Bake prunes prod's retained pins and posts its note before the pipeline turns FAILED; tab 3 shows "
     "it", None, "out", "5b"),
    ("opened at 2026-10-13T14:00:00Z by `demo rearm` (re-take of the approved release; audit record a-1)", "yellow",
     "out", "5a"),
    ("traffic driver started (pid 9603, its own process group; log module11/.local/traffic-r-1.log)", None, "out",
     "5a"),
    (FOOTER, "bold", "out", "F6"),
    (DSP.footer_line(t0=None, now=0.0, item={"state": "WARMING"}, counts={}, verdict_ready=False), "dim", "out", "F1"),
    ("ROLLED_BACK: press v for the verdict", "bold green", "out", "F6"),
    ("traffic driver exited 1 at t0+1:18", "red", "out", "F6"), (race.LADDER_2, "bold red", "out", "F6"),
    ("not armed: demo-state.armed None, control WARMING r-1, -1 outcome item(s). Ladder step 1: stop the take, run "
     "`module11 demo rearm`, re-take `d 5`. No traffic was started.", "bold red", "out", "5a"),
    ("no ROLLED_BACK at t0+5:30 (or PROMOTED): ladder step 4: stop the take, run `module11 demo rearm`, re-take "
     "`d 5`", "bold red", "out", "F6"),
    ("traffic driver exited without finishedAt", "bold red", "out", "F6"),
    ("staging revert failed: exit 3; run module11 drift revert --stage staging after the take", "yellow", "out", "C6"),
    ("staging revert failed: exited without status; run module11 drift revert --stage staging after the take",
     "yellow", "out", "C6"),
    ("no canary session recorded (B5 sets it after the purge)", "yellow", "out", "6a"),
    ("no recommendation recorded yet (the prep chain's recommendation step)", "yellow", "out", "6e"),
    (USER_LINES[2], "red", "out", "0a"), (CANARY_LINES[3], "cyan", "out", "0a"),
    ("# The demo harness's suite deliberately has no payments-specific runbook scenario: that is the gap B5 finds and "
     "B6", None, "out", "4b"),
    ('  preview: {scope: "tag:fast", repeats: {default: 2, adversarial: 2}}   # the 12 scenarios tagged fast (C14)',
     None, "out", "4b"),
]
SUB_ONLY = ("0a", "4b")              # rules that apply only inside their own sub-beat


def _consoles(monkeypatch, width: int = 100) -> list[Console]:
    consoles: list[Console] = []
    monkeypatch.setattr(DSP, "_console", lambda: consoles.append(_console(width)) or consoles[-1])
    return consoles


def _text(consoles: list[Console]) -> str:
    return "\n".join(c.export_text() for c in consoles)


# ---------------------------------------------------------------- the rewrite table
def test_audience_line_rewrites_every_inventoried_string():
    for text, style, kind, sub in RAW:
        for replay in (False, True):
            out = EX.audience_line(text, style, kind, sub, replay=replay)
            assert out is None or out[0] != text, text                     # every inventoried line is rewritten
            assert out is None or EX.internal_hits(out[0]) == [], (text, out)
    shown = {raw[0]: EX.audience_line(*raw) for raw in RAW}
    assert shown["[3c] consistency check, both directions   (C4)"] == ("consistency check, both directions", "bold")
    assert shown["[0a] module11 status --all"] is None and shown["[3f] takeaway"] is None
    assert shown["  3a: not configured (PARTNER_SETUP.md)"] is None          # the B3 line above says it once
    assert shown["(exit 1; press r to replay this beat)"] == ("(the command failed)", "yellow")
    assert EX.audience_line("(exit 1; press r to replay this beat)", "yellow", replay=True) is None
    assert shown[FOOTER][0] == ("elapsed 0:42 | ROLLED_BACK | cand 0 (new 0) · stable 0 | search_runbooks cand 0 vs "
                                "stable 0 | ROLLED_BACK | errors 0 | traffic driver ended early (exit 1)")
    assert shown["ROLLED_BACK: press v for the verdict"] == ("ROLLED_BACK", "bold green")
    assert shown[race.LADDER_2] == ("Stopped.", "bold red")
    assert shown["1c re-take check failed (ClientError: denied); use r for 1c"] == ("Couldn't check the prod stack.",
                                                                                     "dim")
    assert shown["keys here: Enter, s, r"] is None                  # the prompt repeats; no key names on screen
    for text, *_ in RAW:                                            # key cues and stop lines in action words only
        out = EX.audience_line(text, None, "out", "F6")
        assert out is None or not re.search(r"next: |\bkeys\b|press [a-z]\b", out[0]), (text, out)
    assert shown["traffic driver exited 1 at t0+1:18"] == ("The traffic driver ended early (exit 1).", "red")
    assert shown["opened at 2026-10-13T14:00:00Z by `demo rearm` (re-take of the approved release; audit record a-1)"] \
        == ("opened at 2026-10-13T14:00:00Z by `module11 demo rearm`, re-opening the approved release (audit record "
            "a-1)", "yellow")                                                     # same facts, plain words
    for kept in (("$ module11 status --all", "bold green", "cmd", "0a"),
                 ("REPLAY of real run r1, recorded 2026-10-09T19:55:06Z", "bold black on yellow", "banner", None),
                 ("REPLAY of real run r1, recorded 2026-10-09T19:55:06Z", "bold black on yellow", "out", None),
                 ("RAN BEFORE THE SESSION: real run 9b42 at InProgress", "bold magenta", "label", "F1"),
                 ("RAN BEFORE THE SESSION: real run 9b42 at InProgress", "bold magenta", "out", "F1"),
                 ("  Source            Source Succeeded", None, "out", "F1"), (USER_LINES[2], None, "out", "5a")):
        assert EX.audience_line(*kept) == (kept[0], kept[1]), kept      # commands, output and honesty labels verbatim


def test_internal_patterns_catch_key_cues_and_mid_line_ids():
    for dirty in ("ROLLED_BACK · next: v", "elapsed 0:42 | ROLLED_BACK | errors 0 | next: v", "next: t › ",
                  "keys: Enter, s, r", "Stopped.  next: rearm", "ROLLED_BACK: press v for the verdict",
                  "# The demo harness's suite deliberately has no payments-specific runbook scenario: that is the gap "
                  "B5 finds and B6", "  preview: {scope: tag:fast}   # the 12 scenarios tagged fast (C14)",
                  "# closes. Every scenario (F9-98, " + "FEAT" + "-123 F9-99 and F9-97)",
                  "# Gate configuration (design 1.2).", "order that covers a different ambiguity type (F9-96)."):
        assert EX.internal_hits(dirty) == [dirty], dirty
    for clean in ("opened at 2026-10-13T14:00:00Z by `module11 demo rearm`, re-opening the approved release (audit "
                  "record a-1)", "next › ", "next: the verdict › ", "next: tick the controller › ", "ROLLED_BACK",
                  "C5 error: search_runbooks is offered but never referenced", "Next: Module 12 goes deeper",
                  "Stopped: the traffic driver ended early.",
                  "model price: planning price ($5 / $20 per 1M tokens); prices.yaml has none for this "
                  "model (run `module11 prices refresh`)"):
        assert EX.internal_hits(clean) == [], clean                     # the allow-list and real CLI wording


def test_b4_thresholds_listing_trims_comments_in_explain_mode(monkeypatch):
    """4b lists thresholds.yaml (a hashed release artifact, never edited for the demo): explain mode prints its YAML
    without the build's comments plus a `(comments trimmed)` marker; flag off and the recording keep every line."""
    m = driver()
    monkeypatch.setattr(m.Run, "cmd", _fake_cmd([], code=0))
    monkeypatch.setattr(m.S, "read", lambda: {})
    raw = (ROOT / "module11/harness/evaluation/thresholds.yaml").read_text(encoding="utf-8").splitlines()[:30]
    assert EX.internal_hits("\n".join(raw))                              # the file's comments carry internal ids
    for explain in (True, False):
        screen = _screen(120)
        run = m.Run(_args("full", explain), 4, screen, _timings())
        m.beat4(run)
        lines = [line.rstrip() for line in screen._console.export_text().splitlines()]
        recorded = [e["text"] for e in screen.events if e["sub"] == "4b"]
        assert recorded[1] == "--- module11/harness/evaluation/thresholds.yaml"
        assert recorded[2:32] == raw                                      # the recording keeps the raw lines
        if not explain:
            assert _subsequence(raw, lines) and "(comments trimmed)" not in lines
            continue
        assert EX.internal_hits("\n".join(lines)) == [], EX.internal_hits("\n".join(lines))
        at = lines.index("--- module11/harness/evaluation/thresholds.yaml")
        assert lines[at + 1] == "(comments trimmed)"
        code = [re.sub(r"\s+#(\s.*)?$", "", line) for line in raw if not line.lstrip().startswith("#")]
        assert code and _subsequence(code, lines[at + 2:])                # every YAML line, comments cut, in order
        assert not any("#" in line for line in lines[at + 2:at + 2 + len(code)])


def test_b3_without_the_friday_mr_shows_one_plain_line_in_explain_mode(monkeypatch):
    """The driver's `3a: not configured (PARTNER_SETUP.md)` filler lines print only with explain off."""
    m = driver()
    monkeypatch.setattr(m.S, "read", lambda: {})
    for explain in (True, False):
        screen = _screen(120)
        m.beat3(m.Run(_args("full", explain), 3, screen, _timings()))
        text = screen._console.export_text()
        if explain:
            assert "This part needs the Friday merge request, which isn't set up yet." in text
            assert "not configured" not in text and EX.internal_hits(text) == [], text
        else:
            assert text.count("not configured (PARTNER_SETUP.md)") == len(m.SUBBEATS[3])


def test_prompts_are_short_and_neutral(monkeypatch):
    m = driver()
    seen: list[str] = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": seen.append(prompt) or "")
    for n in m.SECTIONS:
        run = m.Run(_args("full", True, no_pause=False), n, _screen(), _timings())
        for sub in m.SUBBEATS[n]:
            run.step(sub, lambda: None)
    literals = re.findall(r'run\.key\("([^"]+)"', Path(m.__file__).read_text(encoding="utf-8"))
    assert len(literals) == 4                       # 1c's r prompt, t0, the t tick and the verdict
    run = m.Run(_args("full", True, no_pause=False), 5, _screen(), _timings())
    for prompt in literals:
        run.key(prompt, "rtv")
    assert len(seen) == sum(len(subs) for subs in m.SUBBEATS.values()) + 4
    for prompt in seen:
        assert len(prompt) <= 30 and EX.internal_hits(prompt) == [] and prompt.startswith("next"), prompt
    assert set(seen[-4:]) == {"next: replay › ", "next: start the traffic › ", "next: tick the controller › ",
                              "next: the verdict › "}                # action words, no key names
    seen.clear()                                    # flag off: today's prompts
    off = m.Run(_args("full", False, no_pause=False), 1, _screen(), _timings())
    off.step(m.SUBBEATS[1][2], lambda: None)
    assert seen == ["[1c] pulumi preview --stack prod --diff --policy-pack (C5): Enter, s skip, r replay > "]


# ---------------------------------------------------------------- live, every beat
def _fake_cmd(lines: list[str], code: int = 1):
    def cmd(self, *argv, **kw):
        self.screen.say(f"$ module11 {' '.join(argv)}", "bold green", kind="cmd")
        for line in lines:
            self.screen.say(line)
        if code:
            self.screen.say(f"(exit {code}; press r to replay this beat)", "yellow")
        return code, "\n".join(lines)
    return cmd


@pytest.mark.parametrize("cut", CUTS)
def test_explain_on_no_internal_string_live_b0_to_b7(monkeypatch, cut):
    m = driver()
    monkeypatch.setattr(m.Run, "cmd", _fake_cmd(["some real output"]))
    for n in m.SECTIONS:
        def beat(run, n=n) -> None:
            for sub in m.SUBBEATS[n]:
                run.step(sub, lambda: run.cmd("x"))
        monkeypatch.setitem(m.SECTIONS, n, (m.TITLES[n], beat))
        consoles = _consoles(monkeypatch)
        assert m.run_section(n, _args(cut, True), _timings()) == 0
        text = _text(consoles)
        assert EX.internal_hits(text) == [], (n, cut)
        active = [s for s in m.SUBBEATS[n] if s.id not in m.skipped(cut)]
        assert ("Part %d of 8" % (n + 1) in text) == bool(active)
        assert text.count("$ module11 x") == len(active) and text.count("(the command failed)") == len(active)
        assert "some real output" in text or not active


def _stub_b5(m, monkeypatch, *, armed: dict, footer_item: dict, child_report=None):
    monkeypatch.setattr(m, "_armed", lambda run: armed)
    monkeypatch.setattr(m, "_control", lambda run, stage: {"rolloutId": "r1", "state": "WARMING"})
    monkeypatch.setattr(m, "_arm_counts", lambda run, stage, rid: {"candidate": {"n": 0}, "stable": {"n": 0}})
    monkeypatch.setattr(m, "_start_traffic", lambda run, rid: None)
    monkeypatch.setattr(m, "_production_rows", lambda run, execution: [])
    monkeypatch.setattr(m, "_label_execution", lambda run, execution: None)
    monkeypatch.setattr(m, "_remember_canary_session", lambda run, rid: None)
    monkeypatch.setattr(m, "bake_line", lambda run: "Bake: stub")
    monkeypatch.setattr(m.S, "read", lambda: {"armed": {"pipelineExecutionId": "e1"}})
    monkeypatch.setattr(race, "check_traffic_child", lambda *a, **k: None)
    monkeypatch.setattr(m.Run, "cmd", _fake_cmd(["real output"], code=0))
    from module11.demo import live_check

    def no_gitlab():
        raise RuntimeError("offline test")
    monkeypatch.setattr(live_check, "_gitlab_client", no_gitlab)

    class Footer(m.Footer):
        def start(self) -> None:
            self.item = dict(footer_item)
            self.child_report = child_report
            self.rows = [DSP.footer_line(t0=self.t0, now=self.t0 + 42, item=self.item, counts={},
                                         verdict_ready=False)]
            self._live = _LiveStub()
    monkeypatch.setattr(m, "Footer", Footer)


@pytest.mark.parametrize("case", ["rolled_back", "not_armed", "child_exited", "no_rollback"])
def test_explain_on_b5_paths_print_plain_words(monkeypatch, case):
    m = driver()
    armed = {} if case == "not_armed" else {"rolloutId": "r1", "openedAt": "t"}
    item = {"state": "ROLLED_BACK" if case == "rolled_back" else "WARMING", "release": "rel", "rolloutId": "r1"}
    report = race.ChildReport(1, "traffic driver exited 1 at t0+1:18", race.LADDER_2, None) \
        if case == "child_exited" else None
    if case == "no_rollback":
        monkeypatch.setattr(m, "STOP_S", 0)
    _stub_b5(m, monkeypatch, armed=armed, footer_item=item, child_report=report)
    consoles = _consoles(monkeypatch)
    code = m.run_section(5, _args("full", True), _timings())
    text = _text(consoles)
    assert EX.internal_hits(text) == [], (case, EX.internal_hits(text))
    assert "Part 6 of 8 · The one that got through: canary and automatic rollback" in text
    expected = {"rolled_back": "ROLLED_BACK",
                "not_armed": "Stopped: the canary isn't armed; no traffic was started.",
                "child_exited": "Stopped: the traffic driver ended early.",
                "no_rollback": "Stopped: no automatic rollback in the time limit."}[case]
    lines = [line.strip() for line in text.splitlines()]
    assert expected in lines and code == (0 if case == "rolled_back" else 3 if case == "not_armed" else 4)
    assert "next: " not in text.replace("next: start the traffic", "") and "rearm" not in text
    if case == "child_exited":
        assert "The traffic driver ended early (exit 1)." in lines and "Stopped." in lines
    if case in ("rolled_back", "no_rollback"):
        assert "elapsed 0:42 | " in text                                    # F6's tick rows


def test_live_footer_renderable_is_audience_clean(monkeypatch):
    m = driver()
    run = SimpleNamespace(explain=True, args=SimpleNamespace(no_live_footer=False), t={}, event=lambda *a, **k: None,
                          screen=SimpleNamespace(_console=None))
    footer = m.Footer(run, "r1", 100.0, None)
    footer.item, footer.verdict_ready = {"state": "ROLLED_BACK"}, True
    captured = {}

    class Live:
        def __init__(self, get_renderable, **kw):
            captured["render"] = get_renderable
            self.console = None

        def start(self):
            pass

        def stop(self):
            pass
    import rich.live
    monkeypatch.setattr(rich.live, "Live", Live)
    monkeypatch.setattr(footer, "poll_once", lambda: None)
    monkeypatch.setattr(m.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    footer.start()
    shown = captured["render"]().plain
    assert shown.startswith("elapsed ") and shown.endswith("| ROLLED_BACK | errors 0") and "next" not in shown
    assert EX.internal_hits(shown) == []
    run.explain = False
    footer.start()
    assert captured["render"]().plain.startswith("t0+")                    # flag off: unchanged


# ---------------------------------------------------------------- replay
def _synthetic_doc(n: int, m) -> dict:
    events = [{"sub": None, "kind": "header", "text": f"{m.TITLES[n]}   (budget 1:00 at cut level full)"}]
    for s in m.SUBBEATS[n]:
        events += [{"sub": s.id, "kind": "out", "style": "bold",
                    "text": f"[{s.id}] {s.title}" + (f"   ({s.cut})" if s.cut else "")},
                   {"sub": s.id, "kind": "cmd", "style": "bold green", "text": f"$ module11 {s.id}"},
                   {"sub": s.id, "kind": "out", "style": None, "text": f"real output of {s.id}"},
                   {"sub": s.id, "kind": "out", "style": "yellow", "text": "(exit 1; press r to replay this beat)"}]
    raw = [{"sub": m.SUBBEATS[n][0].id, "kind": "out", "style": style, "text": text}
           for text, style, kind, sub in RAW if kind != "header" and sub not in SUB_ONLY]
    return {"schema": "m11-capture/1", "capture": f"b{n}", "runIds": ["r1"], "recordedAt": "2026-10-13T00:00:00Z",
            "events": events + raw + [{"sub": m.SUBBEATS[n][0].id, "kind": "label", "style": "bold magenta",
                                       "text": "RAN BEFORE THE SESSION: real run x at y"}]}


def _replay(m, monkeypatch, n: int, path: str, cut: str, explain: bool) -> str:
    consoles = _consoles(monkeypatch)
    assert m.replay_section(n, path, _args(cut, explain), None) == 0
    return _text(consoles)


@pytest.mark.parametrize("cut", CUTS)
def test_explain_on_no_internal_string_replay(tmp_path, monkeypatch, cut):
    m = driver()
    for n in m.SECTIONS:
        path = tmp_path / f"b{n}.json"
        path.write_text(json.dumps(_synthetic_doc(n, m)), encoding="utf-8")
        on = _replay(m, monkeypatch, n, str(path), cut, True)
        off = _replay(m, monkeypatch, n, str(path), cut, False)
        assert EX.internal_hits(on) == [], (n, cut, EX.internal_hits(on))
        assert EX.internal_hits(off)                                      # the synthetic capture is dirty
        assert _subsequence(_verbatim(off.splitlines()), on.splitlines()), (n, cut)   # real output verbatim
        assert on.count("REPLAY of real run") == off.count("REPLAY of real run")
        assert on.count("RAN BEFORE THE SESSION") == off.count("RAN BEFORE THE SESSION")
        for s in m.SUBBEATS[n]:
            kept = s.id not in m.skipped(cut)
            assert (f"$ module11 {s.id}" in on) == kept and (f"real output of {s.id}" in on) == kept


@pytest.mark.parametrize("cut", CUTS)
def test_replay_fixtures_explain_has_no_internal_string(tmp_path, cut):
    result = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--cut", cut, "--explain", env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert EX.internal_hits(result.stdout) == [] and "No recording of this part yet." in result.stdout
    assert "REPLAY of real run" in result.stdout and "How to read this" in result.stdout


# ---------------------------------------------------------------- flag off
@pytest.mark.parametrize("mode, args, extra", [("default", [], {}), ("cut15", ["--cut", "15"], {}),
                                               ("cut12", [], {"M11_CUT": "12"})])
def test_explain_off_unchanged(tmp_path, mode, args, extra):
    """Byte-identical to the pre-change golden (module11/.local/audience-golden, recorded before any edit)."""
    out = run_driver(tmp_path, "--replay-fixtures", "--no-pause", *args, env=_env(tmp_path, **extra))
    assert out.returncode == 0, out.stderr
    golden = GOLDEN / f"fixtures-{mode}.txt"
    if not golden.exists():
        pytest.skip("no local golden (module11/.local is git-ignored)")
    assert out.stdout + out.stderr == golden.read_text(encoding="utf-8")


def test_recording_keeps_the_raw_lines_with_explain_on_or_off(monkeypatch):
    m = driver()
    saved: list[dict] = []
    monkeypatch.setattr(m.RP, "save", lambda doc: saved.append(doc) or ROOT / "module11/.local/captures/b7.json")
    texts = {}
    for explain in (False, True):
        consoles = _consoles(monkeypatch)
        assert m.run_section(7, _args("full", explain, record=True), _timings()) == 0
        texts[explain] = _text(consoles)
    off, on = ([{k: v for k, v in e.items() if k != "t"} for e in doc["events"]] for doc in saved)
    assert off == on and any(e["text"].startswith("B7 Wrap   (budget") for e in on)
    assert "capture written: module11/.local/captures/b7.json" in texts[False]
    assert "capture written" not in texts[True] and EX.internal_hits(texts[True]) == []


def test_timings_written_but_not_printed(tmp_path, monkeypatch, capsys):
    result = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--explain", env=_env(tmp_path))
    assert result.returncode == 0 and "timings:" not in result.stdout and "cut level" not in result.stdout
    assert len(list((tmp_path / "timings").glob("*.json"))) == 1
    m = driver()
    monkeypatch.setenv("M11_TIMINGS_DIR", str(tmp_path / "live"))
    monkeypatch.delenv("M11_EXPLAIN", raising=False)
    assert m.main(["--section", "7", "--no-pause", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "timings:" not in out and "cut level" not in out and "A completed DevOps agent" in out
    [path] = (tmp_path / "live").glob("*.json")
    assert json.loads(path.read_text(encoding="utf-8"))["subBeats"][0]["id"] == "7a"


# ---------------------------------------------------------------- B0: the status table
def test_parse_status_reads_the_real_lines_and_the_canary_line():
    rows = EX.parse_status(USER_LINES)
    assert [(r["stage"], r["state"], r["release"]) for r in rows] == [
        ("dev", "READY", "harness-v19"), ("staging", "READY", "harness-v28"), ("prod", "NOT READY", "harness-v27")]
    assert rows[2]["endpoints"] == [{"name": "candidate", "version": "14", "note": "READY"},
                                    {"name": "stable", "version": "13", "note": None}]
    rows = EX.parse_status(["$ module11 status --all", *CANARY_LINES, "(exit 1; press r to replay this beat)"])
    assert rows[2]["state"] == "CANARY" and rows[2]["rollout"]["pct"] == "50" and rows[2]["rollout"]["n"] == "0"
    assert rows[2]["rollout"]["crel"] == "harness-v28" and rows[2]["rollout"]["cv"] == "14"
    assert EX.parse_status(["\x1b[36m" + CANARY_LINES[2] + "\x1b[0m"])[0]["state"] == "CANARY"
    assert EX.parse_status(["prod     NOT DEPLOYED"])[0]["state"] == "NOT DEPLOYED"
    assert EX.status_view(["nothing here"]) is None


@pytest.mark.parametrize("width", [100, 120])
def test_status_view_is_a_table_with_a_legend(width):
    def render(lines):
        console = _console(width)
        console.print(EX.status_view(lines), soft_wrap=False)
        return console.export_text()
    text = render(CANARY_LINES)
    for header in ("Stage", "Health", "Release", "What's serving", "How to read this"):
        assert header in text
    assert "CANARY" in text and "canary 50%" in text and "harness-v28" in text and "0 sessions" in text
    assert text.index("stable v13") < text.index("candidate v14")             # what users get, first
    assert "2026-10-09T" not in text and "83ce7d4" not in text                # manifest timestamp/commit dropped
    assert "waiting for traffic" in text and all(cell_len(line) <= width for line in text.splitlines())
    legend = text[text.index("How to read this"):].splitlines()
    assert len(legend) - 2 <= 5
    plain = render(USER_LINES)
    assert "waiting for traffic" not in plain and "NOT READY" in plain and "not the recorded version" in plain
    assert not re.search(r"harness/v\d", text)


def test_live_b0_shows_the_table_and_records_the_raw_lines(monkeypatch):
    m = driver()
    monkeypatch.setattr(m.Run, "cmd", _fake_cmd(CANARY_LINES, code=0))
    for explain in (True, False):
        screen = _screen(100)
        run = m.Run(_args("full", explain), 0, screen, _timings())
        m.beat0(run)
        text = screen._console.export_text()
        assert [e["text"] for e in screen.events if e["kind"] == "out"][1:] == CANARY_LINES   # recorded raw
        assert "$ module11 status --all" in text
        if explain:
            assert "What's serving" in text and "canary 50%" in text and EX.internal_hits(text) == []
            assert not any(line in text for line in CANARY_LINES) and "[0a]" not in text
        else:
            assert all(line in text for line in CANARY_LINES) and "What's serving" not in text
            assert "[0a] module11 status --all" in text


def test_fixture_replay_of_b0_shows_the_table(tmp_path):
    on = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--section", "0", "--explain", env=_env(tmp_path))
    off = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--section", "0", env=_env(tmp_path))
    assert on.returncode == off.returncode == 0, on.stderr + off.stderr
    assert "What's serving" in on.stdout and "How to read this" in on.stdout and "operator-52f62fe" in on.stdout
    assert "READY     2026-10-08T21:42:43Z/52f62fe" not in on.stdout            # raw line replaced by the table
    assert "READY     2026-10-08T21:42:43Z/52f62fe" in off.stdout and "What's serving" not in off.stdout
