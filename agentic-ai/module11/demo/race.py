# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The demo driver's process bookkeeping, kept apart so the offline tests drive it with a fake Popen, a fake
clock and a temporary demo-state.json .

- B5: the traffic child. The driver keeps its Popen and calls `check_traffic_child` at every footer poll (5 s,
  also with --no-live-footer) and once more right before 5c's purge. A child that exited without `finishedAt`
  gets `traffic.killedAt` (only while traffic.rolloutId is this rollout and neither finishedAt nor killedAt is
  set), a footer note `traffic driver exited <code> at t0+m:ss`, and the ladder-step-2 prompt, even after
  ROLLED_BACK (5c's purge would otherwise wait on camera until killedAt + 180 s).
- B2 2e / B6: the background staging revert. Every `d 2` writes a fresh `driftRevert` record; `d 6` prints one
  line for this take's backgrounded revert only, from the log's `M11_EXIT` line, then sets `reportedAt`.
Every demo-state.json write goes through state.update; a DemoStateError is the writing step's failure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from module11.demo import run_logged, state as S
from module11.demo.display import clock_text

LADDER_2 = "ladder step 2: stop the take, run `module11 demo rearm`, then re-take `d 5`"
REVERT_FOREGROUND_S = 40
RETAKE_WAIT_S = 120


def _iso(epoch: float) -> str:
    from module11.release.pins import ts
    return ts(epoch).isoformat()


@dataclass
class ChildReport:
    code: int
    note: str
    prompt: str
    killed_at: str | None


def check_traffic_child(child: Any, rollout_id: str, t0: float, *, clock: Callable[[], float],
                        state_path: Path | None = None) -> ChildReport | None:
    """None while the child runs or after a clean exit (finishedAt written); a ChildReport when it exited
    without finishedAt (killedAt written by this finder, unless another finder or the driver got there first)."""
    code = child.poll()
    if code is None:
        return None
    doc = S.read(state_path)                 # a normal or drained driver writes finishedAt before it exits
    rec = doc.get("traffic") or {}
    if rec.get("rolloutId") == rollout_id and rec.get("finishedAt"):
        return None
    found = clock()
    written: dict = {}

    def mark(d: dict) -> dict:
        t = dict(d.get("traffic") or {})
        if t.get("rolloutId") == rollout_id and not t.get("finishedAt") and not t.get("killedAt"):
            t["killedAt"] = _iso(found)
            written["killedAt"] = t["killedAt"]
        return {**d, "traffic": t}
    S.update(mark, state_path)
    after = (S.read(state_path).get("traffic") or {})
    if after.get("finishedAt"):
        return None
    note = f"traffic driver exited {code} at t0+{clock_text(found - t0)}"
    return ChildReport(code, note, LADDER_2, written.get("killedAt") or after.get("killedAt"))


# ---------------------------------------------------------------- B2 2e / B6 revert record
def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_exit(log: str | None) -> tuple[int, str] | None:
    if not log:
        return None
    try:
        return run_logged.parse_exit(Path(log).read_text(encoding="utf-8"))
    except OSError:
        return None


def write_drift_revert(*, pid: int | None, log: str | None, started_at: str | None, backgrounded: bool,
                       state_path: Path | None = None) -> dict:
    """The fresh record every `d 2` writes, replacing any older one."""
    rec = {"pid": pid, "log": log, "startedAt": started_at, "backgrounded": backgrounded, "reportedAt": None}
    S.update(lambda d: {**d, "driftRevert": rec}, state_path)
    return rec


def set_backgrounded(state_path: Path | None = None) -> None:
    S.update(lambda d: {**d, "driftRevert": {**(d.get("driftRevert") or {}), "backgrounded": True}}, state_path)


def retake_check(*, state_path: Path | None = None, alive: Callable[[int | None], bool] = pid_alive,
                 clock: Callable[[], float], sleep: Callable[[float], None]) -> dict | None:
    """A re-take of B2 first reads the old record: an unfinished revert whose pid is alive is waited for up to
    2 minutes. Returns the old record when it is still running after that (2d-2e are then skipped and the fresh
    record keeps its pid, log and startedAt with backgrounded false), else None."""
    old = S.read(state_path).get("driftRevert") or {}
    if not old.get("pid") or log_exit(old.get("log")) is not None or not alive(old.get("pid")):
        return None
    end = clock() + RETAKE_WAIT_S
    while clock() < end:
        sleep(5)
        if log_exit(old.get("log")) is not None or not alive(old.get("pid")):
            return None
    return old


def revert_line(*, state_path: Path | None = None, alive: Callable[[int | None], bool] = pid_alive,
                read_version: Callable[[], Any] = lambda: "?", clock: Callable[[], float]) -> tuple[str | None, dict | None]:
    """B6's first line: printed only for a fresh backgrounded record with reportedAt null. Returns
    (line, C6 timing entry) and sets reportedAt; (None, None) for anything else (stale records never print)."""
    rec = S.read(state_path).get("driftRevert") or {}
    if not rec.get("backgrounded") or rec.get("reportedAt") is not None:
        return None, None
    ended = log_exit(rec.get("log"))
    if ended is not None and ended[0] == 0:
        line, status = f"staging revert: READY on {read_version()}", {"exit": 0, "endedAt": ended[1]}
    elif ended is not None:
        line = (f"staging revert failed: exit {ended[0]}; run module11 drift revert --stage staging after the take")
        status = {"exit": ended[0], "endedAt": ended[1]}
    elif alive(rec.get("pid")):
        line, status = "staging revert still running", {"exit": "running", "endedAt": None}
    else:
        line = ("staging revert failed: exited without status; run module11 drift revert --stage staging after the "
                "take")
        status = {"exit": "no status", "endedAt": None}
    reported = _iso(clock())
    S.update(lambda d: {**d, "driftRevert": {**(d.get("driftRevert") or {}), "reportedAt": reported}}, state_path)
    return line, {"cut": "C6", "line": line, **status, "reportedAt": reported}
