# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Explain mode for the v2 demo driver (module11/demo/explain.py): off by default and then byte-identical; on via
--explain or M11_EXPLAIN=1; one block per non-cut sub-beat on the live and the replay path, never recorded; nothing
printed while B5's live footer redraws; every panel fits 100 and 120 columns; and the on-screen claims stay honest."""
from __future__ import annotations

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
from test_module11_demo_smoke import bare_env, driver, run_driver

MARKERS = ("▸ Step", "💡", "Takeaway", "spotlight", "While the canary runs", " of 8 · ", "How to read this")
WIDTHS = (100, 120)
CUTS = ("full", "15", "12")


def _console(width: int) -> Console:
    return Console(width=width, soft_wrap=True, record=True, highlight=False, color_system=None)


def _args(cut: str = "full", explain: bool = False, **extra) -> SimpleNamespace:
    fields = {"cut": cut, "no_pause": True, "notes": False, "pace": "none", "canary_stage": "prod",
              "explain": explain, "record": False, "no_live_footer": False}
    return SimpleNamespace(**{**fields, **extra})


def _screen(width: int = 100) -> DSP.Screen:
    screen = DSP.Screen(record=True, clock=lambda: 0.0, started=0.0)
    screen._console = _console(width)
    return screen


def _timings() -> dict:
    return {"subBeats": [], "cutPoints": [], "events": []}


def _step_all(m, n: int, cut: str, explain: bool, width: int = 100, footer=None):
    screen = _screen(width)
    run = m.Run(_args(cut, explain), n, screen, _timings())
    if footer is not None:
        run.notes["footer"] = footer
    for sub in m.SUBBEATS[n]:
        run.step(sub, lambda: None)
    return run, screen._console.export_text(), screen.events


def _first_line(sub_id: str) -> str:
    return EX.EXPLAIN[sub_id].text.split("\n")[0][:30]


def _env(tmp_path, **extra):
    return bare_env(tmp_path, COLUMNS="100", **extra)


def _subsequence(small: list[str], big: list[str]) -> bool:
    it = iter(big)
    return all(any(line == other for other in it) for line in small)


# The driver's own labels that explain mode hides or rewrites, listed here independently of explain.py's rules, so a
# rule that over-matches and rewrites real command output fails a test. B0's status lines become a table.
DRIVER_LINES = re.compile("|".join((
    r"^cut level: ", r"^timings: ", r"^B[0-7] .*\(budget \d+:\d\d at cut level \w+\)$",
    r"^\[(?:[0-7][a-z]|F[1-6])\] ", r"^\s+(?:[0-7][a-z]|F[1-6]): not configured \(PARTNER_SETUP\.md\)$",
    r"^\(exit -?\d+[;,] ", r"^capture written: ", r"^no capture \S+ in .* yet: ", r"^keys here: ",
    r"^t0\+", r"^ROLLED_BACK: press v for the verdict$", r"^traffic driver (started|exited) ",
    r"^opened at .* by `demo rearm`", r"^open the branch-alias link ", r"^Bake: .*; tab \d shows it$",
    r"^ladder step 2: ", r"^not armed: ", r"^no ROLLED_BACK at t0\+", r"^1c re-take check failed ",
    r"^a pulumi up can hold the prod stack ", r"^staging (A/B in progress|revert still running|revert failed)",
    r"^2e skipped with 2d$", r"^revert continuing in background; ", r"^B3 needs the Friday MR: ",
    r"^no canary session recorded \(", r"^no recommendation recorded yet \(",
    r"^\S+\s+(READY|NOT READY|CANARY|NOT DEPLOYED)(\s|$)", r"^\s+rollout \S+ \S+, canary ",
)))


def _verbatim(lines: list[str]) -> list[str]:
    """What explain mode must print verbatim and in order: every non-blank flag-off line except the driver's own
    labels (DRIVER_LINES). That is commands, real command output, the REPLAY banner and RAN BEFORE labels."""
    return [line for line in lines if line.strip() and not DRIVER_LINES.search(line)]


def _title(sub) -> str | None:
    """A sub-beat's title line in explain mode (no `[id]`, no cut marker), or None where the command line says it."""
    shown = EX.audience_line(f"[{sub.id}] {sub.title}" + (f"   ({sub.cut})" if sub.cut else ""), "bold")
    return shown[0] if shown else None


# ---------------------------------------------------------------- content
def test_every_sub_beat_and_beat_has_content():
    m = driver()
    assert set(EX.EXPLAIN) == {s.id for subs in m.SUBBEATS.values() for s in subs}
    assert set(EX.BEATS) == set(m.SECTIONS) and EX.LAST_BEAT == max(m.SECTIONS)
    used = [e.concept for e in EX.EXPLAIN.values() if e.concept]
    assert sorted(used) == sorted(EX.CONCEPTS)                 # every concept exists and is shown exactly once
    assert {k for k, e in EX.EXPLAIN.items() if e.partner} == {"3d", "3e", "F1", "F2"}
    assert all(EX.EXPLAIN[f"F{i}"].cue for i in range(1, 7))


# ---------------------------------------------------------------- flag off
@pytest.mark.parametrize("mode", ["default", "15", "12"])
def test_flag_off_is_unchanged(tmp_path, mode):
    args = ["--replay-fixtures", "--no-pause"] + (["--cut", "15"] if mode == "15" else [])
    extra = {"M11_CUT": "12"} if mode == "12" else {}
    unset = run_driver(tmp_path, *args, env=_env(tmp_path, **extra))
    zero = run_driver(tmp_path, *args, env=_env(tmp_path, M11_EXPLAIN="0", **extra))
    assert unset.returncode == zero.returncode == 0, unset.stderr + zero.stderr
    assert unset.stdout == zero.stdout and "REPLAY of real run" in unset.stdout
    assert not any(marker in unset.stdout for marker in MARKERS)


@pytest.mark.parametrize("cut", CUTS)
def test_flag_off_live_steps_print_exactly_the_title_lines(cut):
    m = driver()
    for n in m.SECTIONS:
        _, text, events = _step_all(m, n, cut, explain=False)
        expected = []
        for sub in m.SUBBEATS[n]:
            if sub.id in m.skipped(cut):
                expected.append(f"[{sub.id}] skipped: cut {sub.cut} (level {cut})")
            else:
                expected.append(f"[{sub.id}] {sub.title}" + (f"   ({sub.cut})" if sub.cut else ""))
        assert text.splitlines() == expected
        assert [e["text"] for e in events] == expected


# ---------------------------------------------------------------- the switch
def test_switch_via_flag_and_env(tmp_path, monkeypatch):
    m = driver()
    assert m.parser().parse_args([]).explain is False
    monkeypatch.setenv("M11_EXPLAIN", "1")
    assert m.parser().parse_args([]).explain is True
    monkeypatch.setenv("M11_EXPLAIN", "0")
    assert m.parser().parse_args([]).explain is False
    assert m.parser().parse_args(["--explain"]).explain is True
    monkeypatch.delenv("M11_EXPLAIN")
    for args, extra in ((["--explain"], {}), ([], {"M11_EXPLAIN": "1"})):
        result = run_driver(tmp_path, "--replay-fixtures", "--no-pause", "--section", "0", *args,
                            env=_env(tmp_path, **extra))
        assert result.returncode == 0, result.stderr
        assert "Step 0a" not in result.stdout and _first_line("0a") in result.stdout
        assert "Takeaway" in result.stdout and "Shipping an agent once is easy" in result.stdout
        assert "Part 1 of 8" in result.stdout and "REPLAY of real run" in result.stdout
        assert "B0 of B7" not in result.stdout and "[0a]" not in result.stdout and "budget" not in result.stdout


# ---------------------------------------------------------------- flag on
def _assert_blocks(m, n: int, cut: str, text: str) -> None:
    active = [s.id for s in m.SUBBEATS[n] if s.id not in m.skipped(cut)]
    lines = [line.rstrip() for line in text.splitlines()]
    for sub in m.SUBBEATS[n]:
        label = EX.step_label(sub.id, active)
        if sub.id in active:
            if label:                                                       # one-step beats get no step line
                assert f"▸ {label}" in lines, (sub.id, cut)                 # no title on the step line
                assert f"▸ {label} · " not in text, (sub.id, cut)
            assert _first_line(sub.id) in text, (sub.id, cut)
        else:
            assert _first_line(sub.id) not in text, (sub.id, cut)
        assert f"Step {sub.id}" not in text and f"[{sub.id}]" not in text, (sub.id, cut)   # no letter ids


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("cut", CUTS)
def test_flag_on_renders_a_panel_for_every_non_cut_sub_beat_live(cut, width):
    m = driver()
    for n in m.SECTIONS:
        _, text, events = _step_all(m, n, cut, explain=True, width=width)
        _, _, off_events = _step_all(m, n, cut, explain=False, width=width)
        _assert_blocks(m, n, cut, text)
        assert events == off_events                         # nothing explain-related is recorded


def _synthetic(tmp_path, n: int, m) -> str:
    doc = {"schema": "m11-capture/1", "capture": f"b{n}", "runIds": ["r1"], "recordedAt": "2026-10-13T00:00:00Z",
           "events": [{"sub": None, "kind": "header", "text": "heading"}]
           + [{"sub": s.id, "kind": "out", "text": f"[{s.id}] {s.title}", "style": "bold"} for s in m.SUBBEATS[n]]}
    path = tmp_path / f"syn-b{n}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def _replay(m, monkeypatch, n: int, name: str, cut: str, explain: bool, width: int = 100) -> str:
    consoles: list[Console] = []

    def make():
        consoles.append(_console(width))
        return consoles[-1]
    monkeypatch.setattr(DSP, "_console", make)
    assert m.replay_section(n, name, _args(cut, explain), None) == 0
    return consoles[0].export_text()


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("cut", CUTS)
def test_flag_on_renders_a_panel_for_every_non_cut_sub_beat_replay(tmp_path, monkeypatch, cut, width):
    m = driver()
    for n in m.SECTIONS:
        name = _synthetic(tmp_path, n, m)
        on = _replay(m, monkeypatch, n, name, cut, True, width)
        off = _replay(m, monkeypatch, n, name, cut, False, width)
        _assert_blocks(m, n, cut, on)
        assert on.count("REPLAY of real run") == off.count("REPLAY of real run")
        assert _subsequence(_verbatim(off.splitlines()), on.splitlines())
        active = [s.id for s in m.SUBBEATS[n] if s.id not in m.skipped(cut)]
        assert (f"Part {n + 1} of 8" in on) == bool(active) and ("Takeaway" in on) == bool(active)
        assert EX.internal_hits(on) == [], (n, cut)


@pytest.mark.parametrize("cut", CUTS)
def test_flag_on_keeps_commands_and_honesty_labels(tmp_path, cut):
    args = ["--replay-fixtures", "--no-pause", "--cut", cut]
    off = run_driver(tmp_path, *args, env=_env(tmp_path))
    on = run_driver(tmp_path, *args, "--explain", env=_env(tmp_path))
    assert off.returncode == on.returncode == 0, off.stderr + on.stderr
    verbatim = _verbatim(off.stdout.splitlines())        # commands, real output and honesty labels, in order
    assert any(not line.startswith(("$ ", "REPLAY of real run")) for line in verbatim)   # real output is in it
    assert _subsequence(verbatim, on.stdout.splitlines())
    assert on.stdout.count("REPLAY of real run") == off.stdout.count("REPLAY of real run")
    assert on.stdout.count("RAN BEFORE THE SESSION") == off.stdout.count("RAN BEFORE THE SESSION")


def test_b4_at_cut_12_prints_no_header_and_no_takeaway(tmp_path, monkeypatch):
    m = driver()
    on = _replay(m, monkeypatch, 4, _synthetic(tmp_path, 4, m), "12", True)
    assert "Part 5 of 8" not in on and "Takeaway" not in on and "▸ Step" not in on
    monkeypatch.setattr(DSP, "_console", lambda: _console(100))
    rec = []
    monkeypatch.setattr(DSP.Screen, "show", lambda self, r: rec.append(r))
    assert m.run_section(4, _args("12", True), _timings()) == 0
    assert rec == []


def test_run_section_shows_the_header_block_and_takeaway(monkeypatch):
    m = driver()
    consoles: list[Console] = []
    monkeypatch.setattr(DSP, "_console", lambda: consoles.append(_console(100)) or consoles[-1])
    assert m.run_section(7, _args("full", True), _timings()) == 0
    text = consoles[0].export_text()
    assert text.index("Part 8 of 8") < text.index(_first_line("7a")) < text.index("A completed DevOps agent") \
        < text.index("Takeaway")
    assert "[7a]" not in text and "▸ Step" not in text and "budget" not in text      # one step: no step line


# ---------------------------------------------------------------- B5's live footer
class _LiveStub:
    def stop(self) -> None:
        pass


def test_no_explain_output_while_the_live_footer_is_active():
    m = driver()
    run, text, _ = _step_all(m, 5, "full", explain=True, footer=SimpleNamespace(live_active=True))
    for sub in m.SUBBEATS[5]:
        assert _title(sub) is None or _title(sub) in text
        assert f"[{sub.id}]" not in text and _first_line(sub.id) not in text
    assert not any(marker in text for marker in MARKERS)
    assert run.explain_deferred == ["canary", "shadow", "latch", "side_effects"]
    run, text, _ = _step_all(m, 5, "full", explain=True, footer=SimpleNamespace(live_active=False))
    _assert_blocks(m, 5, "full", text)
    assert run.explain_deferred == []


def test_footer_live_active_tracks_rich_live():
    m = driver()
    footer = m.Footer(SimpleNamespace(), "r1", 0.0, None)
    assert footer.live_active is False
    footer._live = _LiveStub()
    assert footer.live_active is True
    footer.stop()
    assert footer.live_active is False


def test_race_preview_lists_the_fill_items_only_in_explain_mode():
    m = driver()
    for explain in (True, False):
        screen = _screen(100)
        m.Run(_args("full", explain), 5, screen, _timings()).explain_race_preview()
        text = screen._console.export_text()
        if explain:
            for i in range(1, 7):
                assert f"•  {EX.EXPLAIN[f'F{i}'].cue}" in text and f"F{i} " not in text
            assert "Then: the verdict" in text
        else:
            assert text == ""
        assert screen.events == []


def _fake_b5(m, monkeypatch, no_live_footer: bool):
    """beat5 end to end with every AWS, GitLab and traffic read stubbed; the footer's Live is a stub."""
    monkeypatch.setattr(m, "_armed", lambda run: {"rolloutId": "r1", "openedAt": "t"})
    monkeypatch.setattr(m, "_control", lambda run, stage: {"rolloutId": "r1", "state": "WARMING"})
    monkeypatch.setattr(m, "_arm_counts", lambda run, stage, rid: {"candidate": {"n": 0}, "stable": {"n": 0}})
    monkeypatch.setattr(m, "_start_traffic", lambda run, rid: None)
    monkeypatch.setattr(m, "_production_rows", lambda run, execution: [])
    monkeypatch.setattr(m, "_label_execution", lambda run, execution: None)
    monkeypatch.setattr(m, "_remember_canary_session", lambda run, rid: None)
    monkeypatch.setattr(m, "bake_line", lambda run: "Bake: stub")
    monkeypatch.setattr(m.S, "read", lambda: {"armed": {"pipelineExecutionId": "e1"}})
    monkeypatch.setattr(race, "check_traffic_child", lambda *a, **k: None)
    monkeypatch.setattr(m.Run, "cmd", lambda self, *a, **k: (0, ""))
    from module11.demo import live_check

    def no_gitlab():
        raise RuntimeError("offline test")
    monkeypatch.setattr(live_check, "_gitlab_client", no_gitlab)

    class Footer(m.Footer):
        def start(self) -> None:
            self.item = {"state": "ROLLED_BACK", "release": "rel", "rolloutId": "r1"}
            if not self.run.args.no_live_footer:
                self._live = _LiveStub()
    monkeypatch.setattr(m, "Footer", Footer)
    screen = _screen(100)
    rec = screen._console
    run = m.Run(_args("full", True, no_live_footer=no_live_footer), 5, screen, _timings())
    shown: list[bool] = []
    real_show = screen.show

    def show(renderable) -> None:
        footer = run.notes.get("footer")
        shown.append(bool(footer is not None and footer.live_active))
        real_show(renderable)
    screen.show = show
    m.beat5(run)
    return run, rec.export_text(), shown


def test_beat5_prints_nothing_explain_related_under_the_live_footer(monkeypatch):
    m = driver()
    run, text, shown = _fake_b5(m, monkeypatch, no_live_footer=False)
    assert shown and not any(shown)                 # every explain print happened with the footer not live
    assert "▸ Step 1 of 10" in text and "While the canary runs" in text
    subs = {s.id: s for s in m.SUBBEATS[5]}
    assert text.index("▸ Step 1 of 10") < text.index("While the canary runs") < text.index(_title(subs["F1"]))
    for k in range(2, 11):
        assert f"Step {k} of 10" not in text                      # no step lines under the live footer
    for sub in ("F1", "F2", "F3", "F4", "F5", "F6", "5b", "5c", "5d"):
        assert f"[{sub}]" not in text and f"Step {sub}" not in text
        assert _title(subs[sub]) is None or _title(subs[sub]) in text
    assert "ROLLED_BACK" in [line.strip() for line in text.splitlines()]
    assert "press v" not in text and "next: v" not in text
    assert EX.internal_hits(text) == []
    assert run.explain_deferred == ["shadow", "latch", "side_effects"]


@pytest.mark.parametrize("live", [True, False])
def test_r_during_the_race_replays_without_panels(monkeypatch, live):
    """`r` at any B5 prompt, footer live (F1-F5) or stopped (F6, 5b-5d): the fallback replay prints no panels."""
    m = driver()

    def beat(run) -> None:
        run.notes["footer"] = SimpleNamespace(live_active=live)
        raise m.Replay()
    monkeypatch.setitem(m.SECTIONS, 5, (m.TITLES[5], beat))
    monkeypatch.setattr(DSP, "_console", lambda: _console(100))
    seen: list[tuple] = []
    monkeypatch.setattr(m, "replay_section",
                        lambda n, name, args, d: seen.append((args.explain, args.audience)) or 0)
    shows: list = []
    monkeypatch.setattr(DSP.Screen, "show", lambda self, r: shows.append(r))
    assert m.run_section(5, _args("full", True), _timings()) == 0
    assert seen == [(False, True)] and len(shows) == 1     # panels off, clean labels on; the beat header only


def test_r_during_a_live_beat_replays_without_panels(tmp_path, monkeypatch):
    """The in-take fallback replays an older capture, so no beat's `r` path shows panels beside it."""
    m = driver()
    for n in m.SECTIONS:
        capture = _synthetic(tmp_path, n, m)

        def beat(run) -> None:
            raise m.Replay()
        monkeypatch.setitem(m.SECTIONS, n, (m.TITLES[n], beat))
        monkeypatch.setattr(m.RP, "capture_name", lambda section, canary=False, path=capture: path)
        consoles: list[Console] = []
        monkeypatch.setattr(DSP, "_console", lambda: consoles.append(_console(100)) or consoles[-1])
        assert m.run_section(n, _args("full", True), _timings()) == 0
        assert len(consoles) == 2                                # the live beat, then the replay
        replay = consoles[1].export_text()
        assert "REPLAY of real run" in replay and f"[{m.SUBBEATS[n][0].id}]" not in replay   # labels stay clean
        assert not any(marker in replay for marker in MARKERS), n
        assert EX.internal_hits(replay) == [], n


def test_replay_cli_keeps_panels_on(tmp_path):
    """`--replay CAPTURE` (preview.sh) is not the in-take fallback: with --explain it renders every panel."""
    m = driver()
    (tmp_path / "caps").mkdir()
    capture = tmp_path / "caps" / "b3.json"
    capture.write_text(Path(_synthetic(tmp_path, 3, m)).read_text(encoding="utf-8"), encoding="utf-8")
    result = run_driver(tmp_path, "--replay", str(capture), "--no-pause", "--explain", env=_env(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "REPLAY of real run" in result.stdout and "Part 4 of 8" in result.stdout and "Takeaway" in result.stdout
    _assert_blocks(m, 3, "full", result.stdout)


def test_beat5_with_no_live_footer_prints_every_block_inline(monkeypatch):
    m = driver()
    run, text, shown = _fake_b5(m, monkeypatch, no_live_footer=True)
    assert shown and not any(shown)
    _assert_blocks(m, 5, "full", text)
    assert run.explain_deferred == []


# ---------------------------------------------------------------- layout at recording font sizes
def _render(renderable, width: int) -> list[str]:
    screen = DSP.Screen()
    screen._console = _console(width)
    screen.show(renderable)
    return screen._console.export_text().splitlines()


def _check_panel(lines: list[str], width: int) -> int:
    assert lines[0].startswith("╭") and lines[-1].startswith("╰")
    for line in lines:
        assert line[0] in "╭│╰" and line[-1] in "╮│╯" and cell_len(line) == width, line
    return len(lines) - 2


@pytest.mark.parametrize("width", WIDTHS)
def test_panels_fit_a_large_recording_font(width):
    m = driver()
    ids = {s.id: [x.id for x in subs] for subs in m.SUBBEATS.values() for s in subs}
    for sub_id, entry in EX.EXPLAIN.items():
        lines = _render(EX.sub_block(sub_id, ids[sub_id]), width)
        assert all(cell_len(line) <= width for line in lines)
        assert lines[0] == ""
        first = 1
        if EX.step_label(sub_id, ids[sub_id]):
            assert lines[1].startswith("▸ Step") and not lines[1].endswith("…")
            first = 2
        else:
            assert len(ids[sub_id]) == 1 and not lines[1].startswith("▸")      # B0 and B7: one step, no line
        start = first + 1 if entry.cue else first
        if entry.cue:
            assert lines[first].startswith("▶ ") and not lines[first].endswith("…")
        end = next(i for i, line in enumerate(lines) if line.startswith("╰"))
        body = _check_panel(lines[start:end + 1], width)
        assert body <= 3, (sub_id, body)
        concept = lines[end + 1:]
        assert bool(concept) == bool(entry.concept) and len(concept) <= 3, (sub_id, concept)
    for key in EX.CONCEPTS:
        assert len(_render(EX.concept_box(key), width)) <= 3
    for n in m.SECTIONS:
        header = _render(EX.beat_header(n, m.TITLES[n]), width)
        assert len(header) == 2 and f"Part {n + 1} of 8" in header[0] and all(cell_len(x) <= width for x in header)
        assert "LIVE RACE" not in header[0] and f"B{n} " not in header[0]
        for cut in CUTS:
            assert _check_panel(_render(EX.takeaway(n, cut), width), width) <= 3
    assert _check_panel(_render(EX.race_preview(), width), width) <= 9


# ---------------------------------------------------------------- accuracy
def _strings() -> dict[str, str]:
    out = {f"concept {k}": f"{t} {d}" for k, (t, d) in EX.CONCEPTS.items()}
    out |= {f"explain {k}": " ".join(x for x in (e.text, e.title, e.cue or "") if x) for k, e in EX.EXPLAIN.items()}
    out |= {f"beat {n}": " ".join(x for x in (b.story, b.takeaway, b.takeaway_12 or "")) for n, b in EX.BEATS.items()}
    return out


def test_on_screen_text_is_honest():
    strings = _strings()
    for where, text in strings.items():
        low = text.lower()
        assert "24 hour" not in low and "24-hour" not in low and "two-person" not in low, where
        assert not re.search(r"harness/v\d|\bv\d{2}\b|#\d+|!\d+", text), where   # the driver prints live values
        if "two people" in low:
            assert where == "explain F1" and "ExtendedApproval" in text, where
    assert "two people" in strings["explain F1"]
    assert "half the sessions on a compressed demo profile" in EX.CONCEPTS["canary"][1]
    assert "production profile takes hours" in EX.CONCEPTS["canary"][1]
    assert "TrajectoryInOrderMatch" in EX.EXPLAIN["3d"].text
    assert "its own log group, plus a single s3:GetObject on one S3 prefix" in EX.EXPLAIN["1d"].text  # policy order
    assert "a shadow run" in EX.EXPLAIN["F4"].text and "a shadow run" in EX.EXPLAIN["F4"].cue
    assert "assertion" not in strings["beat 3"].lower()       # !7 failed on the trajectory row only
    for entry in EX.EXPLAIN.values():
        assert not entry.cue or not re.search(r"\btab\b|http|://", entry.cue)
