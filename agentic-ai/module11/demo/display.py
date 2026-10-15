# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Terminal output for the Module 11 demo driver (c.3): headers, the RAN BEFORE THE SESSION label,
the persistent REPLAY banner, the recorder behind `--record`, and B5's live footer.

Every line the driver prints goes through `Screen.say`, so `--record` captures exactly what the audience saw
(timestamped stdout events, plus the masked API responses used for rendering) and `--replay` renders the same
events under the yellow banner `REPLAY of real run <ids>, recorded <timestamp>`.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

RAN_BEFORE = "RAN BEFORE THE SESSION: real run {id} at {ts}"
REPLAY_BANNER = "REPLAY of real run {ids}, recorded {ts}"
DEMO_SETTINGS = ("Demo settings (labelled): rolloutProfile demo, teamConcurrency 14, onlineEval.samplingPct 100; "
                 "production defaults are prod, 10 and 10.")
SYNTHETIC = "Synthetic incidents: the traffic driver sends scripted payments and platform reports through the real edge."
STAND_IN = "stand-in: this Gateway target is ours"


def _console():
    try:
        from rich.console import Console
        return Console(highlight=False, soft_wrap=True)
    except ImportError:  # pragma: no cover - rich is pinned
        return None


@dataclass
class Screen:
    """Prints, records and (in replay) re-renders the driver's output."""
    record: bool = False
    banner: str | None = None                  # set in replay: printed before every block
    clock: Callable[[], float] = time.time
    events: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    sub: str | None = None
    started: float = field(default_factory=time.time)
    # explain mode's audience filter (explain.audience_line): (text, style, kind, sub) -> (text, style) to print,
    # a list of them, or None to print nothing. The recording always keeps the raw text.
    clean: Callable[[str, str | None, str, str | None],
                    tuple[str, str | None] | list[tuple[str, str | None]] | None] | None = None
    _console: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._console = _console()

    def _print(self, text: str, style: str | None) -> None:
        if self._console is None:
            print(text, flush=True)
        else:
            self._console.print(text, style=style, markup=False)

    def say(self, text: str = "", style: str | None = None, *, kind: str = "out") -> None:
        with self._lock:
            shown = (text, style) if self.clean is None else self.clean(text, style, kind, self.sub)
            for line in (shown if isinstance(shown, list) else [shown] if shown is not None else []):
                self._print(*line)
            if self.record:
                self.events.append({"t": round(self.clock() - self.started, 2), "sub": self.sub, "kind": kind,
                                    "text": text, "style": style})

    def show(self, renderable: Any) -> None:
        """Print a rich renderable for the audience only (explain mode, module11/demo/explain.py). Never recorded:
        captures stay identical whether explain mode is on, and `--explain` re-renders the panels on replay.
        soft_wrap=False, because the screen console soft-wraps and would crop a panel's body to one line."""
        with self._lock:
            if self._console is not None:
                self._console.print(renderable, soft_wrap=False)

    def header(self, text: str) -> None:
        if self.banner:
            self.say(self.banner, "bold black on yellow", kind="banner")
        self.say("")
        self.say(text, "bold cyan", kind="header")

    def ran_before(self, run_id: str, at: str) -> None:
        self.say(RAN_BEFORE.format(id=run_id, ts=at), "bold magenta", kind="label")

    def api(self, name: str, response: Any) -> None:
        """Keep an API response used for rendering (masked on export, release/redact.py)."""
        if self.record:
            self.responses.append({"t": round(self.clock() - self.started, 2), "sub": self.sub, "api": name,
                                   "response": json.loads(json.dumps(response, default=str))})


def replay_banner(ids: list[str], recorded_at: str) -> str:
    return REPLAY_BANNER.format(ids=", ".join(ids) or "unknown", ts=recorded_at)


def clock_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def footer_line(*, t0: float | None, now: float, item: dict | None, counts: dict, verdict_ready: bool,
                child_note: str | None = None) -> str:
    """`t0+m:ss | state | cand n (new) · stable n | search_runbooks cand x vs stable y, p | verdict | errors`."""
    item = item or {}
    c, s = counts.get("candidate", {}), counts.get("stable", {})
    p = ((item.get("lastSignals") or {}).get("tool_selection_p")) or (item.get("lastP") or "")
    state = item.get("state", "?")
    verdict = f"breach {item.get('breaches', 0)}/2" if state in ("WARMING", "OBSERVING") else state
    if state == "OBSERVING" and not item.get("breaches"):
        verdict = f"clean {item.get('cleanTicks', 0)}/3"
    line = (f"t0+{clock_text(now - t0) if t0 else '-:--'} | {state} | cand {c.get('n', 0)} (new {c.get('new', 0)}) "
            f"· stable {s.get('n', 0)} | search_runbooks cand {c.get('runbooks', 0)} vs stable {s.get('runbooks', 0)}"
            f"{', p ' + str(p) if p else ''} | {verdict} | errors {c.get('errors', 0) + s.get('errors', 0)}")
    if state == "ROLLED_BACK" and verdict_ready:
        line += " | press v for the verdict"
    if child_note:
        line += f" | {child_note}"
    return line


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
