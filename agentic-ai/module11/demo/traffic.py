# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 traffic`: synthetic incidents through a stage's real edge.

Two modes:
- the S7 shape, `--sessions 180 --concurrency 20 --mix payments=0.6`: one worker pool per team, sized
  round(concurrency x mix) (12 payments, 8 platform), each under the stage's `teamConcurrency`;
- `--until-terminal --rate <n/min>`: Poisson-timed sessions until the stage's rollout is PROMOTED or
  ROLLED_BACK (or `--max-minutes`, exit 4), for pipeline promotions.

Sessions go API Gateway -> Cognito (Module 10's terminal client, USER_PASSWORD_AUTH from
M11_MODULE10_CREDENTIALS_FILE; passwords are never printed) -> the m11 router -> AgentCore. A
`team-concurrency` 429 is retried after 2 s up to 3 times and counted.

Transport failures follow the runners' rule (evaluation/runner.py `transport_reinvokes`): a session is re-sent only
when it is provable that it never reached the container. Through the edge that proof is not available: the
adapter answers a failed InvokeAgentRuntime with a bare 502 "Invocation failed" and writes the outcome item. So
the driver never re-sends one. It counts them (`invocationFailed`, with request IDs) and the summary shows them;
the controller sees each one in the arm's error rate, as a user would.

Before its first session the driver waits until the control item is WARMING or OBSERVING with the expected
rolloutId (and says why at its deadline, exit 3). It is then the only writer of the `traffic` start record in
`module11/.local/demo-state.json` and replaces that key wholesale: `{rolloutId, stage, pid, startedAt,
deadlineSeconds}`, where deadlineSeconds is `granted.deadlineSeconds` of the candidate's own pins object. Every
later write (`finishedAt`, `stopReason`) is a compare-and-set on rolloutId and pid. On SIGTERM/SIGINT it stops
starting sessions, waits for in-flight ones up to deadlineSeconds + 30 s, and writes `finishedAt` with
`stopReason: signal`. A failed state write is logged and exits 1 (the caller treats it as a crash).
"""
from __future__ import annotations

import json
import math
import queue
import random
import signal
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from module11.release import pins as P

from . import state as S

LABEL = ("Synthetic incidents from the fixture library, sent through the real production path "
         "(API Gateway \u2192 Cognito \u2192 router \u2192 AgentCore).")
ARMED_STATES = ("WARMING", "OBSERVING", "SHADOWING")    # SHADOWING: a model rollout's or the experiment's shadow
TERMINAL = ("PROMOTED", "ROLLED_BACK")
RETRY_429_SECONDS = 2.0
RETRIES_429 = 3
DEFAULT_WAIT_SECONDS = 30.0

# The fixture library: incident reports per team. Every one is answered with the runbook on stable
# (search_runbooks in each scenario's expected trajectory), so the payments edit shows as a tool-selection shift.
FIXTURES = {
    "payments": [
        "payments-service started timing out after this morning's deploy.",
        "payments-service authorizations are timing out since this morning.",
        "Summarize the payments-service incident for the on-call channel.",
        "payments-service latency doubled. Is it the fraud check?",
    ],
    "platform": [
        "The checkout-service is throwing 502s since the 14:20 deploy.",
        "Why are customers seeing errors at checkout-service? Keep it short.",
        "checkout-service error rate jumped after the latest deploy. What is the cause?",
    ],
}


class TrafficError(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def is_invocation_failed(session: "Session") -> bool:
    """The edge's answer to a failed InvokeAgentRuntime: HTTP 502 with no session status (adapter.mjs)."""
    return session.httpStatus == 502 and session.status == "http-502"


def parse_mix(text: str | None, teams: tuple[str, ...] = ("payments", "platform")) -> dict[str, float]:
    """`payments=0.6` -> {"payments": 0.6, "platform": 0.4}; the unnamed team gets the rest."""
    mix: dict[str, float] = {}
    for part in filter(None, (text or "").split(",")):
        team, _, value = part.partition("=")
        team = team.strip()
        if team not in teams:
            raise ValueError(f"unknown team {team!r} in --mix (teams: {', '.join(teams)})")
        share = float(value)
        if not 0.0 <= share <= 1.0:
            raise ValueError("--mix shares must be between 0 and 1")
        mix[team] = share
    rest = [t for t in teams if t not in mix]
    left = 1.0 - sum(mix.values())
    if left < -1e-9:
        raise ValueError("--mix shares add up to more than 1")
    for team in rest:
        mix[team] = max(0.0, left) / len(rest)
    return {t: mix[t] for t in teams}


def pool_sizes(concurrency: int, mix: Mapping[str, float]) -> dict[str, int]:
    """One worker pool per team: round(concurrency x share) (12 and 8 for 20 x 0.6/0.4)."""
    if concurrency < 1:
        raise ValueError("--concurrency must be at least 1")
    sizes = {team: int(math.floor(concurrency * share + 0.5)) for team, share in mix.items()}
    return {team: n for team, n in sizes.items() if n > 0}


def check_team_concurrency(sizes: Mapping[str, int], team_concurrency: int) -> None:
    """The driver refuses to start when a pool is larger than the stage's teamConcurrency (preflight item 15)."""
    largest = max(sizes.values()) if sizes else 0
    if largest > int(team_concurrency):
        raise TrafficError(f"the largest worker pool ({largest}) exceeds the stage's teamConcurrency "
                           f"({team_concurrency}); lower --concurrency or raise the stage setting", 2)


def split_sessions(total: int, mix: Mapping[str, float]) -> dict[str, int]:
    """Per-team session counts that add up to `total` (largest remainder)."""
    raw = {t: total * s for t, s in mix.items()}
    out = {t: int(math.floor(v)) for t, v in raw.items()}
    for team in sorted(raw, key=lambda t: raw[t] - out[t], reverse=True)[:total - sum(out.values())]:
        out[team] += 1
    return out


@dataclass
class Session:
    team: str
    requestId: str
    summary: str
    startedAt: float = 0.0
    endedAt: float = 0.0
    httpStatus: int = 0
    status: str = "pending"
    retries429: int = 0

    @property
    def seconds(self) -> float:
        return round(self.endedAt - self.startedAt, 2)


@dataclass
class Ports:
    """Everything the driver touches, injectable for tests."""
    send: Callable[[str, str, str], tuple[int, Mapping]]        # (team, summary, request id) -> (HTTP status, body)
    read_control: Callable[[], Mapping | None]
    candidate_pins: Callable[[], Mapping]                       # the candidate's own pins object
    state_path: Path | None = None
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = print
    rng: random.Random = field(default_factory=random.Random)
    pid: int | None = None


class Driver:
    def __init__(self, ports: Ports, *, stage: str, mix: Mapping[str, float], concurrency: int,
                 team_concurrency: int, expected_rollout: str | None = None) -> None:
        self.p, self.stage, self.mix = ports, stage, dict(mix)
        self.sizes = pool_sizes(concurrency, mix)
        check_team_concurrency(self.sizes, team_concurrency)
        self.expected = expected_rollout
        self.rollout_id: str | None = None
        self.deadline_seconds = 0
        self.pid = ports.pid if ports.pid is not None else __import__("os").getpid()
        self.stop = threading.Event()
        self.signalled = threading.Event()
        self.sessions: list[Session] = []
        self.retries429 = 0
        self._lock = threading.Lock()
        self.in_flight = 0

    # -------------------------------------------------------- arming
    def wait_armed(self, wait_seconds: float = DEFAULT_WAIT_SECONDS, poll: float = 2.0) -> str:
        """Block until WARMING/OBSERVING with the expected rolloutId; TrafficError(exit 3) at the deadline."""
        end = self.p.clock() + wait_seconds
        why = "no control item"
        while True:
            item = self.p.read_control() or {}
            state, rid = item.get("state"), item.get("rolloutId")
            if state in ARMED_STATES and (self.expected is None or rid == self.expected):
                return rid
            why = (f"rollout {rid} is {state}" if self.expected is None or rid == self.expected
                   else f"the control item names rollout {rid} ({state}), not {self.expected}")
            if self.p.clock() >= end:
                raise TrafficError(f"not armed after {int(wait_seconds)} s: {why}; no session sent", 3)
            self.p.sleep(poll)

    def record_start(self) -> dict:
        """Replace demo-state.json's `traffic` key wholesale: this driver is its only writer."""
        self.deadline_seconds = P.granted_deadline_seconds(self.p.candidate_pins())
        rec = {"rolloutId": self.rollout_id, "stage": self.stage, "pid": self.pid, "startedAt": S.now_iso(),
               "deadlineSeconds": self.deadline_seconds}
        S.update(lambda doc: {**doc, "traffic": rec}, self.p.state_path)
        return rec

    def record_finish(self, reason: str, **extra: Any) -> bool:
        """Compare-and-set on rolloutId and pid; a record that belongs to someone else is left unchanged."""
        owner: dict = {}

        def fn(doc: dict) -> dict:
            rec = doc.get("traffic")
            if not isinstance(rec, dict) or rec.get("rolloutId") != self.rollout_id or rec.get("pid") != self.pid:
                owner["rec"] = rec if isinstance(rec, dict) else {}
                return doc
            return {**doc, "traffic": {**rec, "finishedAt": S.now_iso(), "stopReason": reason, **extra}}
        S.update(fn, self.p.state_path)
        if "rec" in owner:
            rec = owner["rec"]
            self.p.log(f"WARN traffic record belongs to {rec.get('rolloutId')}/{rec.get('pid')}; not written")
            return False
        return True

    # -------------------------------------------------------- sessions
    def _one(self, session: Session) -> None:
        with self._lock:
            self.in_flight += 1
        session.startedAt = self.p.clock()
        try:
            for attempt in range(RETRIES_429 + 1):
                status, body = self.p.send(session.team, session.summary, session.requestId)
                if status == 429 and (body or {}).get("source") == "team-concurrency" and attempt < RETRIES_429:
                    session.retries429 += 1
                    with self._lock:
                        self.retries429 += 1
                    self.p.sleep(RETRY_429_SECONDS)
                    continue
                break
            session.httpStatus = int(status)
            session.status = str((body or {}).get("status") or ("http-" + str(status)))
        except Exception as exc:  # noqa: BLE001 - a failed session is recorded, the driver keeps going
            session.httpStatus, session.status = 0, f"error:{type(exc).__name__}"
        finally:
            session.endedAt = self.p.clock()
            with self._lock:
                self.in_flight -= 1
                self.sessions.append(session)

    def _new(self, team: str) -> Session:
        return Session(team, f"m11-traffic-{uuid.uuid4().hex[:20]}", self.p.rng.choice(FIXTURES[team]))

    def _pools(self, queues: dict[str, queue.Queue]) -> list[threading.Thread]:
        threads = []
        for team, size in self.sizes.items():
            for _ in range(size):
                def work(q=queues[team]):
                    while not self.stop.is_set():
                        try:
                            item = q.get(timeout=0.5)
                        except queue.Empty:
                            if getattr(q, "closed", False):
                                return
                            continue
                        if item is None:
                            return
                        if self.stop.is_set():
                            return
                        self._one(item)
                t = threading.Thread(target=work, daemon=True)
                t.start()
                threads.append(t)
        return threads

    def _drain(self, threads: list[threading.Thread]) -> int:
        """Wait for in-flight sessions up to deadlineSeconds + 30 s; returns how many were still running."""
        end = self.p.clock() + self.deadline_seconds + 30
        for t in threads:
            t.join(timeout=max(0.0, end - self.p.clock()))
        return self.in_flight

    def run_fixed(self, total: int) -> dict:
        counts = split_sessions(total, self.mix)
        queues = {team: queue.Queue() for team in self.sizes}
        for team, n in counts.items():
            for _ in range(n):
                queues[team].put(self._new(team))
        for team, size in self.sizes.items():
            for _ in range(size):
                queues[team].put(None)
        threads = self._pools(queues)
        while any(t.is_alive() for t in threads) and not self.signalled.is_set():
            time.sleep(0.2)
        return self._finish(threads)

    def run_until_terminal(self, rate_per_minute: float, max_minutes: float, poll: float = 5.0) -> dict:
        queues = {team: queue.Queue() for team in self.sizes}
        threads = self._pools(queues)
        teams, weights = zip(*[(t, s) for t, s in self.mix.items() if t in self.sizes])
        started = self.p.clock()
        next_arrival = started
        next_poll = started
        outcome = None
        while not self.signalled.is_set():
            now = self.p.clock()
            if now >= next_poll:
                item = self.p.read_control() or {}
                if item.get("rolloutId") == self.rollout_id and item.get("state") in TERMINAL:
                    outcome = {"state": item["state"], "reason": item.get("reason")}
                    break
                next_poll = now + poll
            if now - started >= max_minutes * 60:
                outcome = {"state": "TIMEOUT"}
                break
            if now >= next_arrival:
                team = self.p.rng.choices(teams, weights=weights)[0]
                queues[team].put(self._new(team))
                next_arrival = now + self.p.rng.expovariate(rate_per_minute / 60.0)
            self.p.sleep(max(0.05, min(next_arrival, next_poll) - self.p.clock()))
        for q in queues.values():
            q.closed = True
        result = self._finish(threads)
        result["outcome"] = outcome
        return result

    def _finish(self, threads: list[threading.Thread]) -> dict:
        signalled = self.signalled.is_set()
        if signalled:
            self.stop.set()
        still = self._drain(threads)
        extra = {"inFlightAtDeadline": still} if signalled else {}
        written = self.record_finish("signal" if signalled else "complete", **extra)
        return {"sessionCount": len(self.sessions), "retries429": self.retries429, "stopReason":
                "signal" if signalled else "complete", "inFlight": still, "recordWritten": written}

    def handle_signals(self) -> None:
        def on_signal(signum, _frame):
            self.signalled.set()
            self.stop.set()
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)

    def summary(self) -> dict:
        by_team: dict[str, dict] = {}
        for s in self.sessions:
            row = by_team.setdefault(s.team, {"sessions": 0, "completed": 0})
            row["sessions"] += 1
            row["completed"] += s.status == "completed"
        durations = sorted(s.seconds for s in self.sessions if s.httpStatus == 200)
        failed = [s.requestId for s in self.sessions if is_invocation_failed(s)]
        return {"teams": by_team, "retries429": self.retries429,
                "invocationFailed": {"count": len(failed), "requestIds": failed, "reSent": 0},
                "medianSeconds": durations[len(durations) // 2] if durations else None,
                "sessions": [asdict(s) for s in self.sessions]}


def run(ports: Ports, *, stage: str, mix: Mapping[str, float], concurrency: int, team_concurrency: int,
        sessions: int | None = None, until_terminal: bool = False, rate: float = 20.0, max_minutes: float = 120.0,
        expected_rollout: str | None = None, wait_seconds: float = DEFAULT_WAIT_SECONDS,
        out_dir: Path | None = None, install_signals: bool = True) -> tuple[int, dict]:
    """The whole command; returns (exit code, summary)."""
    log = ports.log
    log(LABEL)
    driver = Driver(ports, stage=stage, mix=mix, concurrency=concurrency, team_concurrency=team_concurrency,
                    expected_rollout=expected_rollout)
    log(f"pools: {', '.join(f'{t} {n} workers' for t, n in driver.sizes.items())} (teamConcurrency "
        f"{team_concurrency})")
    if install_signals:
        driver.handle_signals()
    try:
        driver.rollout_id = driver.wait_armed(wait_seconds)
    except TrafficError as exc:
        log(str(exc))
        return exc.code, {}
    try:
        rec = driver.record_start()
    except (S.DemoStateError, P.PinsViolation) as exc:
        log(f"ERROR traffic start record not written: {exc}; no session sent")
        return 1, {}
    log(f"rollout {driver.rollout_id}: traffic started at {rec['startedAt']} (pid {driver.pid}, "
        f"deadlineSeconds {rec['deadlineSeconds']})")
    try:
        result = (driver.run_until_terminal(rate, max_minutes) if until_terminal else driver.run_fixed(int(sessions)))
    except S.DemoStateError as exc:
        log(f"ERROR traffic finish record not written: {exc}")
        return 1, driver.summary()
    summary = {**driver.summary(), **result, "rolloutId": driver.rollout_id, "stage": stage}
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"traffic-{driver.rollout_id}.json").write_text(json.dumps(summary, indent=2, default=str),
                                                                   encoding="utf-8")
    teams = ", ".join(f"{t} {r['completed']}/{r['sessions']} completed" for t, r in summary["teams"].items())
    log(f"sessions: {result['sessionCount']} ({teams}); team-concurrency 429 retries: {result['retries429']}; "
        f"invocation failed (not re-sent, no pre-dispatch proof at the edge): {summary['invocationFailed']['count']}; "
        f"stop: {result['stopReason']}")
    outcome = result.get("outcome")
    if until_terminal:
        if not outcome or outcome.get("state") == "TIMEOUT":
            log(f"rollout {driver.rollout_id} not terminal after {max_minutes:g} minutes")
            return 4, summary
        log(f"rollout {driver.rollout_id}: {outcome['state']} ({outcome.get('reason') or 'promoted'})")
    return 0, summary
