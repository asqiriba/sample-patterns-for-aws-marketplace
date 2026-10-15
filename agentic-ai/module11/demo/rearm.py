# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 demo rearm`: re-run the approved release's Production stage for a B5 re-take.

Order (every check before any write; steps 2-4 idempotent, so a re-run after exit 4 or 5 resumes):
  1 validate: demo-state.armed {rolloutId, pipelineExecutionId, tag} (else 2); the stage's rolloutProfile is
    `demo`, the execution's Seal RELEASE_TAG equals armed.tag, the caller is an operator principal and no other
    rollout is active (else 3)
  2 stop this rollout's traffic driver (SIGTERM, drain wait, SIGKILL, killedAt) and settle the rollout:
    non-terminal -> abort and wait <= 2 min for ROLLED_BACK; PROMOTED -> 2; ROLLED_BACK -> go on;
    IDLE -> go on only with a rearm record for armed.rolloutId (a re-run after exit 4/5), else 2
  3 wait <= 15 min for the Production stage to be Failed (Bake pruned and posted its note)
  4 latch clear, flags restore, purge (purge-wait rule, h.4), controller reset; then demo-state.rearm
  5 a `demo_rearm` audit record (exit 5 if it fails: the retry never runs unaudited)
  6 GetCallerIdentity again, then RetryStageExecution(ALL_ACTIONS)
  7 wait <= 15 min for WARMING with a new rolloutId; update armed, clear rearm, run `live_check --demo --armed`

Exit codes: 0 re-armed and green; 2 nothing to re-arm; 3 a guard refused; 4 a wait timed out, cleanup is
incomplete, the retry was refused for now or the credentials failed before it; 5 the audit write failed;
6 the retry was refused for good; 7 no rollout opened after the retry; 8 re-armed but the preflight is red.

Guards are defense in depth for a published command; IAM remains the boundary (OPERATIONS.md: demo stacks only).
`module11 demo arm --stage dev --rearm` (demo/arm.py) reuses `stop_driver` and `cleanup`.
"""
from __future__ import annotations

import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from module11.demo import state as S

PIPELINE = "m11-release"
PRODUCTION = "Production"
ROLLOUT_ID = re.compile(r"^r-[0-9a-f]{16}$")
EXECUTION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TAG = re.compile(r"^harness/v[0-9]+$")
NON_TERMINAL = ("WARMING", "OBSERVING", "ADVANCING", "ROLLING_BACK", "SHADOWING", "PROMOTING")
ABORT_WAIT_S = 120
STAGE_WAIT_S = 15 * 60
ARM_WAIT_S = 15 * 60
POLL_S = 10
KILL_POLL_S = 10
RETRY_CONFLICT = ("ConflictException", "ConcurrentPipelineExecutionsLimitExceededException")
RETRY_FOR_GOOD = ("StageNotRetryableException", "NotLatestPipelineExecutionException")
FULL_RESET = ("run the full reset cycle (OPERATIONS.md: `demo prep --reset`, then `module11 demo prep --payments` with a "
              "harness-owner approval, the staging gate, then Approve + Run)")


class Exit(Exception):
    def __init__(self, code: int, message: str, level: str = "ERROR") -> None:
        super().__init__(message)
        self.code, self.message, self.level = code, message, level


@dataclass
class Ports:
    """Everything the command touches, so the offline tests run it against fakes."""
    stage: str
    state_path: Path
    rollout_profile: Callable[[], str]                  # the stage stack's rolloutProfile output
    caller_arn: Callable[[], str]                       # sts:GetCallerIdentity ARN (may raise)
    operator_principals: Sequence[str]
    read_control: Callable[[], Mapping | None]          # the control item as DynamoDB returns it (Decimals)
    controller: Callable[[str, dict], Mapping]          # invoke the controller Lambda: command, args -> {"ok": ...}
    latch_clear: Callable[[str, str], None]             # behavior digest, reason (audited; no-op when not latched)
    flags_restore: Callable[[], None]
    candidate_pins: Callable[[Mapping], Mapping]        # the rolled-back candidate's own pins object
    delete_records: Callable[[str], bool]               # release -> True when every note's record is gone
    audit: Callable[[str, dict], str]                   # kind, fields -> record id (ledger, release/audit.py)
    alive: Callable[[int], bool]                        # is any process of this group running
    killpg: Callable[[int, int], None]
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    log: Callable[[str, str], None] = lambda level, line: None
    pipeline: Any = None                                # a codepipeline client (rearm)
    live_check_armed: Callable[[], bool] | None = None  # rearm step 7
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- shared helpers
def _say(ports: Ports, line: str, level: str = "INFO") -> None:
    ports.log(level, line)


def _state(ports: Ports) -> dict:
    try:
        return S.read(ports.state_path)
    except (OSError, ValueError) as exc:
        raise Exit(4, f"demo-state.json unreadable: {exc}; fix or remove it") from exc


def _update(ports: Ports, fn, step: str) -> dict:
    try:
        return S.update(fn, ports.state_path)
    except S.DemoStateError as exc:
        raise Exit(4, f"cleanup incomplete at {step}: {exc}; nothing retried; re-run the command") from exc


def _iso(epoch: float) -> str:
    from module11.release.pins import ts
    return ts(epoch).isoformat()


def _is_operator(arn: str, principals: Sequence[str]) -> bool:
    if arn in principals:
        return True
    # An assumed-role session ARN matches its role's ARN in operatorPrincipals.
    m = re.match(r"^arn:aws:sts::(\d+):assumed-role/([^/]+)/", arn)
    return bool(m) and f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}" in principals


def stop_driver(ports: Ports, rollout_id: str, *, wait: bool = True, step: str = "driver stop") -> str:
    """Stop this rollout's traffic driver by c.6 step 2's rule. Returns what happened (for the log).
    wait=False sends SIGTERM only (the abort follows, then call again with wait=True)."""
    rec = (_state(ports).get("traffic") or {})
    if rec.get("rolloutId") != rollout_id or rec.get("finishedAt") or rec.get("killedAt"):
        return "no running driver for this rollout"
    pid = rec.get("pid")
    if not isinstance(pid, int):
        return "no driver pid recorded"
    if not ports.alive(pid):
        found = ports.clock()

        def dead(doc: dict) -> dict:
            t = dict(doc.get("traffic") or {})
            if t.get("rolloutId") == rollout_id and not t.get("finishedAt") and not t.get("killedAt"):
                t["killedAt"] = _iso(found)
            return {**doc, "traffic": t}
        _update(ports, dead, step)
        _say(ports, f"traffic driver {pid} found dead without finishedAt; killedAt {_iso(found)}", "WARN")
        return "found dead"
    if not rec.get("stopRequestedAt"):
        sent = ports.clock()
        ports.killpg(pid, signal.SIGTERM)
        _update(ports, lambda doc: {**doc, "traffic": {**(doc.get("traffic") or {}), "stopRequestedAt": _iso(sent)}}
                if (doc.get("traffic") or {}).get("rolloutId") == rollout_id else doc, step)
        _say(ports, f"SIGTERM sent to traffic driver group {pid}; it drains and writes finishedAt")
        requested = sent
    else:
        from module11.release.pins import ts
        requested = ts(rec["stopRequestedAt"]).timestamp()
    if not wait:
        return "SIGTERM sent"
    deadline = int(rec.get("deadlineSeconds") or 150)
    while ports.clock() < requested + deadline + 60:
        now_rec = _state(ports).get("traffic") or {}
        if now_rec.get("rolloutId") != rollout_id or now_rec.get("finishedAt"):
            return "drained"
        ports.sleep(min(5.0, requested + deadline + 60 - ports.clock()))
    now_rec = _state(ports).get("traffic") or {}
    if now_rec.get("finishedAt"):
        return "drained"
    killed = ports.clock()
    ports.killpg(pid, signal.SIGKILL)
    while ports.alive(pid) and ports.clock() < killed + KILL_POLL_S:
        ports.sleep(1)
    if ports.alive(pid):
        _say(ports, f"traffic driver {pid} survived SIGKILL; killedAt not written (the purge will give up)", "WARN")
        return "survived SIGKILL"

    def mark(doc: dict) -> dict:
        t = dict(doc.get("traffic") or {})
        if t.get("rolloutId") == rollout_id and not t.get("finishedAt") and not t.get("killedAt"):
            t["killedAt"] = _iso(killed)
        return {**doc, "traffic": t}
    _update(ports, mark, step)
    _say(ports, f"traffic driver {pid} killed at {_iso(killed)} after {deadline + 60} s without finishedAt", "WARN")
    return "killed"


def wait_state(ports: Ports, states: tuple, timeout: float, *, rollout_id: str | None = None,
               not_rollout: str | None = None) -> Mapping | None:
    end = ports.clock() + timeout
    while True:
        item = ports.read_control() or {}
        if item.get("state") in states and (rollout_id is None or item.get("rolloutId") == rollout_id) \
                and (not_rollout is None or item.get("rolloutId") != not_rollout):
            return item
        if ports.clock() >= end:
            return None
        ports.sleep(min(POLL_S, max(0.0, end - ports.clock())) or POLL_S)


def purge(ports: Ports, item: Mapping, release: str) -> None:
    """Design 6.6 step 7 under h.4's purge-wait rule, in-process (memory.purge_wait); raises Exit(4) on a give-up."""
    from module11.aws import memory
    from module11.cli_ops import release_rollout
    from module11.release import pins as P
    try:
        pins = ports.candidate_pins(item)
        ready = memory.purge_wait(lambda: release_rollout(dict(ports.read_control() or {}), release),
                                  lambda: S.read(ports.state_path).get("traffic"), pins,
                                  clock=ports.clock, sleep=ports.sleep, log=lambda line: _say(ports, line))
    except (memory.PurgeNotReady, P.PinsViolation) as exc:
        raise Exit(4, f"cleanup incomplete at purge ({type(exc).__name__}: {exc}); nothing retried; "
                      f"re-run the command") from exc
    _say(ports, f"purge ready ({ready.case}) after {ready.waited_seconds:.0f} s")
    try:
        complete = ports.delete_records(release)
    except Exception as exc:  # noqa: BLE001 - any delete failure is an incomplete cleanup
        raise Exit(4, f"cleanup incomplete at purge ({type(exc).__name__}: {exc}); nothing retried; "
                      f"re-run the command") from exc
    if not complete:
        raise Exit(4, "cleanup incomplete at purge (records left or failed deletes); nothing retried; re-run the command")


def cleanup(ports: Ports, item: Mapping, reason: str) -> None:
    """Design 6.6 steps 5-8 for the rolled-back rollout `item` (each a no-op when already done)."""
    digest = str(item.get("behaviorDigest") or "")
    release = str(item.get("release") or "")
    for step, fn in (("latch clear", lambda: ports.latch_clear(digest, reason) if digest else None),
                     ("flags restore", ports.flags_restore)):
        try:
            fn()
        except Exit:
            raise
        except Exception as exc:  # noqa: BLE001
            raise Exit(4, f"cleanup incomplete at {step} ({type(exc).__name__}: {exc}); nothing retried; "
                          f"re-run the command") from exc
        _say(ports, f"{step}: done")
    purge(ports, item, release)
    current = ports.read_control() or {}
    if current.get("state") != "IDLE":
        try:
            result = ports.controller("reset", {"reason": reason})
        except Exception as exc:  # noqa: BLE001
            raise Exit(4, f"cleanup incomplete at reset ({type(exc).__name__}: {exc}); nothing retried; "
                          f"re-run the command") from exc
        if not result.get("ok"):
            raise Exit(4, f"cleanup incomplete at reset ({result}); nothing retried; re-run the command")
    _say(ports, "controller reset: IDLE")


def settle(ports: Ports, item: Mapping, rollout_id: str, reason: str, *, promoted_code: int = 2) -> Mapping:
    """Step 2: stop the driver and bring a non-terminal rollout to ROLLED_BACK; returns the terminal item."""
    state = item.get("state")
    if state in NON_TERMINAL:
        stop_driver(ports, rollout_id, wait=False)
        result = ports.controller("abort", {"reason": reason})
        if not result.get("ok"):
            _say(ports, f"abort answered {result}", "WARN")
        done = wait_state(ports, ("ROLLED_BACK",), ABORT_WAIT_S, rollout_id=rollout_id)
        if done is None:
            raise Exit(4, "rollout did not reach ROLLED_BACK; check the m11-rollback-stuck alarm")
        stop_driver(ports, rollout_id)
        return done
    if state == "PROMOTED":
        raise Exit(promoted_code, f"the armed release was PROMOTED: {FULL_RESET}", "ERROR")
    stop_driver(ports, rollout_id)
    return item


# ---------------------------------------------------------------- CodePipeline reads
def actions(pipeline: Any, execution_id: str) -> list[dict]:
    out, token = [], None
    while True:
        page = pipeline.list_action_executions(pipelineName=PIPELINE, filter={"pipelineExecutionId": execution_id},
                                               **({"nextToken": token} if token else {}))
        out += page.get("actionExecutionDetails") or []
        token = page.get("nextToken")
        if not token:
            return out


def _latest(rows: list[dict], stage: str) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in rows:
        if row.get("stageName") != stage:
            continue
        name = row.get("actionName")
        if name not in latest or str(row.get("startTime")) >= str(latest[name].get("startTime")):
            latest[name] = row
    return latest


def stage_status(rows: list[dict], stage: str = PRODUCTION) -> str:
    """InProgress | Failed | Succeeded | NotStarted for one execution's stage (its newest attempt per action)."""
    latest = _latest(rows, stage)
    if not latest:
        return "NotStarted"
    statuses = {r.get("status") for r in latest.values()}
    if "InProgress" in statuses:
        return "InProgress"
    if statuses & {"Failed", "Abandoned"}:
        return "Failed"
    return "Succeeded"


def action_status(rows: list[dict], action: str, stage: str = PRODUCTION) -> str | None:
    row = _latest(rows, stage).get(action)
    return row.get("status") if row else None


def seal_tag(rows: list[dict]) -> str | None:
    row = _latest(rows, "Seal").get("Seal") or {}
    return ((row.get("output") or {}).get("outputVariables") or {}).get("RELEASE_TAG")


def _code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None


# ---------------------------------------------------------------- the command
def validate(ports: Ports) -> tuple[dict, Mapping]:
    armed = _state(ports).get("armed") or {}
    rid, eid, tag = armed.get("rolloutId"), armed.get("pipelineExecutionId"), armed.get("tag")
    if not (isinstance(rid, str) and ROLLOUT_ID.match(rid) and isinstance(eid, str) and EXECUTION_ID.match(eid)
            and isinstance(tag, str) and TAG.match(tag)):
        raise Exit(2, "nothing armed to re-arm; run the full reset cycle", "WARN")
    if ports.rollout_profile() != "demo":
        raise Exit(3, "demo rearm refuses outside the demo rollout profile")
    try:
        ports.pipeline.get_pipeline_execution(pipelineName=PIPELINE, pipelineExecutionId=eid)
        recorded = seal_tag(actions(ports.pipeline, eid))
    except Exception as exc:  # noqa: BLE001 - not found, or unreadable: not the armed release
        raise Exit(3, f"execution {eid} is not the armed release ({_code(exc) or type(exc).__name__})") from exc
    if recorded != tag:
        raise Exit(3, f"execution {eid} is not the armed release (Seal recorded {recorded}, armed {tag})")
    if not _is_operator(ports.caller_arn(), ports.operator_principals):
        raise Exit(3, "caller is not an operator principal")
    item = ports.read_control() or {}
    if item.get("state") in NON_TERMINAL and item.get("rolloutId") != rid:
        raise Exit(3, "a different rollout is active")
    return armed, item


def rearm(ports: Ports, reason: str = "demo retake") -> int:
    try:
        return _rearm(ports, reason)
    except Exit as exc:
        _say(ports, exc.message, exc.level)
        return exc.code


def _rearm(ports: Ports, reason: str) -> int:
    armed, item = validate(ports)
    rid, eid, tag = armed["rolloutId"], armed["pipelineExecutionId"], armed["tag"]
    state = item.get("state", "IDLE")
    resumed = False
    if state == "IDLE" or item.get("rolloutId") != rid:
        record = _state(ports).get("rearm") or {}
        if not (record.get("armedRolloutId") == rid and record.get("cleanedAt")):
            raise Exit(2, f"control item is {state} and nothing shows it was cleaned up for {rid}; "
                          "run the full reset cycle", "WARN")
        resumed = True
        _say(ports, f"resuming: {rid} was cleaned up at {record['cleanedAt']} (step 4 skipped)")
        prior_state = state
    else:
        prior_state = state
        item = settle(ports, item, rid, reason)
    # Step 3: Bake prunes and posts its note before the Production stage turns Failed.
    end = ports.clock() + STAGE_WAIT_S
    while True:
        status = stage_status(actions(ports.pipeline, eid))
        if status == "Failed":
            break
        if ports.clock() >= end:
            bake = action_status(actions(ports.pipeline, eid), "Bake")
            raise Exit(4, f"Production stage not Failed after 15 minutes (Bake {bake}); only the abort and the "
                          "driver stop happened; re-run module11 demo rearm")
        ports.sleep(POLL_S)
    _say(ports, "Production stage Failed (Bake pruned and posted its note)")
    # Step 4: cleanup, then the rearm record.
    if not resumed:
        cleanup(ports, item, reason)
        cleaned = _iso(ports.clock())
        _update(ports, lambda doc: {**doc, "rearm": {"armedRolloutId": rid, "cleanedAt": cleaned}}, "reset")
    # Step 5: audit before any retry.
    caller = ports.caller_arn()
    try:
        record_id = ports.audit("demo_rearm", {
            "pipelineExecutionId": eid, "tag": tag, "behaviorDigest": item.get("behaviorDigest"), "reason": reason,
            "caller": caller, "priorRolloutId": rid, "priorState": prior_state})
    except Exception as exc:  # noqa: BLE001
        raise Exit(5, f"audit write failed ({type(exc).__name__}: {exc}); nothing retried; re-run module11 demo rearm") \
            from exc
    _say(ports, f"audit record {record_id}")
    # Step 6: credentials again, then one retry call (Conflict/ConcurrentLimit retried 3x).
    try:
        ports.caller_arn()
    except Exception as exc:  # noqa: BLE001
        raise Exit(4, "credentials invalid before the retry; refresh them and re-run") from exc
    attempts = 0
    while True:
        attempts += 1
        try:
            ports.pipeline.retry_stage_execution(pipelineName=PIPELINE, stageName=PRODUCTION,
                                                 pipelineExecutionId=eid, retryMode="ALL_ACTIONS")
            _say(ports, f"RetryStageExecution({PRODUCTION}, {eid}, ALL_ACTIONS) accepted")
            break
        except Exception as exc:  # noqa: BLE001
            code = _code(exc)
            if code is None:
                _say(ports, f"RetryStageExecution outcome unknown ({type(exc).__name__}: {exc}); not repeated; "
                            "observing the stage", "WARN")
                break
            if code in RETRY_FOR_GOOD:
                raise Exit(6, f"RetryStageExecution refused ({code}): {FULL_RESET}") from exc
            if code in RETRY_CONFLICT:
                if attempts <= 3:
                    ports.sleep(10)
                    continue
                raise Exit(4, f"retry refused for now ({code}); re-run module11 demo rearm") from exc
            raise Exit(6, f"RetryStageExecution failed ({code}): {exc}; {FULL_RESET}") from exc
    # Step 7: a new WARMING rollout.
    opened = wait_state(ports, ("WARMING",), ARM_WAIT_S, not_rollout=rid)
    if opened is None:
        deploy = next((r for r in reversed(actions(ports.pipeline, eid))
                       if r.get("stageName") == PRODUCTION and r.get("actionName") == "Deploy"), {})
        build = ((deploy.get("output") or {}).get("executionResult") or {}).get("externalExecutionId")
        raise Exit(7, f"Production stage retried but no rollout opened; see m11-deploy build {build}; if it opens "
                      "later, `live_check --demo --armed` records it")
    new = {"rolloutId": opened["rolloutId"], "pipelineExecutionId": eid, "tag": tag,
           "openedAt": _iso(opened.get("openedAt") or ports.clock()), "rearmedBy": "demo rearm",
           "auditRecord": record_id}

    def rearmed(doc: dict) -> dict:
        doc = {**doc, "armed": new}
        doc.pop("rearm", None)
        return doc
    _update(ports, rearmed, "armed")
    _say(ports, f"re-armed: rollout {new['rolloutId']} WARMING for {tag} (audit record {record_id})")
    if ports.live_check_armed is not None and not ports.live_check_armed():
        _say(ports, "the --armed preflight is red; fix the red items above", "WARN")
        return 8
    return 0
