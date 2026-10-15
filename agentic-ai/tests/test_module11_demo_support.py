# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared fakes for the `demo rearm` / `demo arm` / demo driver tests (no tests here; the name keeps it in the
`tests/test_module11_*` set). The fake control table holds items exactly as `controller.decide(...)` writes them
(epoch-second floats for openedAt/rolledBackAt/promotedAt) and returns every number as a Decimal, as DynamoDB
does ."""
from __future__ import annotations

import json
import signal
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from module11.demo import state as S
from module11.release import controller as ctl

RID = "r-00112233445566aa"
RID2 = "r-99887766554433bb"
EID = "0a1b2c3d-1111-2222-3333-444455556666"
TAG = "harness/v16"
DIGEST = "sha256:" + "ab" * 32
OPERATOR = "arn:aws:iam::111122223333:user/operator"
T0 = 1792508400.0                        # 2026-10-20T15:00:00Z
PINS = {"granted": {"deadlineSeconds": 150}}


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def ddb(item):
    return json.loads(json.dumps(item, default=str), parse_float=Decimal, parse_int=Decimal)


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = float(t)
        self.hooks: list = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds)) or 0.001
        for hook in list(self.hooks):
            hook(self.t)


OPEN_ARGS = {"rolloutId": RID, "profile": "demo", "stableVersion": 7, "candidateVersion": 8,
             "behaviorDigest": DIGEST, "manifestVersion": "2026-10-20T14:00:00Z/abcdef1", "release": "harness-v16"}


class FakeControl:
    """The control table plus the controller Lambda: commands go through `controller.decide`."""

    def __init__(self, clock: Clock, state: str = "WARMING", rollout_id: str = RID, rolled_back_ago: float = 290,
                 open_args: dict | None = None) -> None:
        self.clock = clock
        self.item = None
        self.calls: list[tuple[str, dict]] = []
        self.rollback_lands_after = 30.0     # seconds after abort until the READY poll answers
        self.never_rolls_back = False
        self._abort_at = None
        base = {"state": "IDLE", "revision": 3, "endpoints": {"stable": {"version": 7}}}
        clock.hooks.append(self._tick)
        if state == "IDLE":
            self.item = base
            return
        args = {**OPEN_ARGS, "rolloutId": rollout_id, **(open_args or {})}
        item = ctl.decide(base, "open", args, None, None, clock.t - 1800).item
        if state == "ROLLED_BACK":
            item = ctl.decide(item, "abort", {"reason": "tool_selection"}, None, None,
                              clock.t - rolled_back_ago - 10).item
            item = self._ready(item, clock.t - rolled_back_ago)
        elif state == "PROMOTED":
            item = {**item, "state": "PROMOTED", "promotedAt": clock.t - 200}
        self.item = item

    def _ready(self, item, at):
        obs = {"endpoints": {"candidate": {"status": "READY", "liveVersion": 7}}}
        return ctl.decide(item, "tick", {}, None, None, at, obs).item

    def open_later(self, delay: float, rollout_id: str = RID2) -> None:
        """What an accepted RetryStageExecution does: m11-deploy opens a new rollout `delay` s later."""
        at = self.clock.t + delay

        def hook(now: float) -> None:
            if now >= at and self.item.get("rolloutId") != rollout_id:
                self.item = ctl.decide(self.item, "open", {**OPEN_ARGS, "rolloutId": rollout_id}, None, None, now).item
                self.clock.hooks.remove(hook)
        self.clock.hooks.append(hook)

    def _tick(self, now: float) -> None:
        if self.never_rolls_back:
            return
        if self._abort_at is not None and self.item["state"] == "ROLLING_BACK" \
                and now >= self._abort_at + self.rollback_lands_after:
            self.item = self._ready(self.item, now)

    def read(self):
        return ddb(self.item) if self.item is not None else None

    def invoke(self, command: str, args: dict) -> dict:
        self.calls.append((command, dict(args)))
        if command == "abort":
            self.item = ctl.decide(self.item, "abort", args, None, None, self.clock.t).item
            self._abort_at = self.clock.t
        elif command == "reset":
            self.item = ctl.decide(self.item, "reset", args, None, None, self.clock.t).item
        elif command == "open":
            self.item = ctl.decide(self.item, "open", args, None, None, self.clock.t).item
        return {"ok": True, "state": self.item["state"]}


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


class FakePipeline:
    def __init__(self, clock: Clock, *, tag: str = TAG, production: str = "Failed") -> None:
        self.clock = clock
        self.tag = tag
        self.production = production       # Failed | InProgress (turns Failed after fail_after s)
        self.fail_after = None
        self.retry_errors: list = []         # exceptions raised by successive retry calls
        self.retries: list[dict] = []
        self.on_retry = None                 # fn(): what happens after an accepted retry (opens a rollout)
        self.started = clock.t

    def get_pipeline_execution(self, pipelineName, pipelineExecutionId):
        if pipelineExecutionId != EID:
            raise ClientError("PipelineExecutionNotFoundException")
        return {"pipelineExecution": {"pipelineExecutionId": EID, "status": "Failed"}}

    def list_action_executions(self, pipelineName, filter, nextToken=None):
        status = self.production
        if status == "InProgress" and self.fail_after is not None and self.clock.t >= self.started + self.fail_after:
            status = "Failed"
        rows = [{"stageName": "Seal", "actionName": "Seal", "status": "Succeeded", "startTime": "1",
                 "output": {"outputVariables": {"RELEASE_TAG": self.tag}}},
                {"stageName": "Production", "actionName": "Deploy", "status": "Succeeded", "startTime": "2",
                 "output": {"executionResult": {"externalExecutionId": "m11-deploy:build-1"}}},
                {"stageName": "Production", "actionName": "Bake", "status": status, "startTime": "3"}]
        return {"actionExecutionDetails": rows}

    def retry_stage_execution(self, **kw):
        self.retries.append(kw)
        if self.retry_errors:
            err = self.retry_errors.pop(0)
            if err is not None:
                raise err
        if self.on_retry:
            self.on_retry()
        return {"pipelineExecutionId": EID}


class FakeProcs:
    """A process table for the traffic driver's group. `term` decides what SIGTERM does:
    'drain' writes finishedAt after `drain_s`, 'ignore' does nothing; `survive_kill` keeps the group after SIGKILL."""

    def __init__(self, clock: Clock, state_path: Path, *, pid: int = 4242, alive: bool = True, term: str = "drain",
                 drain_s: float = 20.0, survive_kill: bool = False) -> None:
        self.clock, self.path, self.pid = clock, state_path, pid
        self.running = alive
        self.term, self.drain_s, self.survive_kill = term, drain_s, survive_kill
        self.signals: list[tuple[int, float]] = []
        self._drain_at = None
        clock.hooks.append(self._tick)

    def alive(self, pid: int) -> bool:
        return pid == self.pid and self.running

    def killpg(self, pid: int, sig: int) -> None:
        self.signals.append((sig, self.clock.t))
        if sig == signal.SIGTERM and self.term == "drain":
            self._drain_at = self.clock.t + self.drain_s
        if sig == signal.SIGKILL and not self.survive_kill:
            self.running = False

    def _tick(self, now: float) -> None:
        if self._drain_at is not None and now >= self._drain_at and self.running:
            self.running = False
            self._drain_at = None
            S.update(lambda doc: {**doc, "traffic": {**doc["traffic"], "finishedAt": iso(now),
                                                     "stopReason": "signal"}}, self.path)


def traffic_record(state_path: Path, *, rollout_id: str = RID, pid: int = 4242, started: float = T0 - 120,
                   **extra) -> None:
    S.update(lambda doc: {**doc, "traffic": {"rolloutId": rollout_id, "pid": pid, "startedAt": iso(started),
                                             "deadlineSeconds": 150, "stage": "prod", **extra}}, state_path)


class Recorder:
    """Ordered record of every side-effecting call."""

    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        self.logs: list[tuple[str, str]] = []

    def call(self, name: str, *args):
        self.events.append((name, *args))
        if name in self.fail:
            raise self.fail.pop(name)

    def names(self) -> list[str]:
        return [e[0] for e in self.events]

    def log(self, level: str, line: str) -> None:
        self.logs.append((level, line))

    def text(self) -> str:
        return "\n".join(line for _, line in self.logs)
