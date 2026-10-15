# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rollback controller: the pure state machine and its orchestration.

`decide(item, command, args, profile, log, now, obs)` is pure: it takes the control item
(`m11-<stage>-control {pk: ROLLOUT, sk: CURRENT}`), a command, the profile, a `signals.Log` of
the rollout's sessions and the latest observations (endpoint status, shadow batches), and
returns a `Decision`: the next item and the side effects to perform, in order.

`Controller` runs a decision against injected ports (the Lambda handler wires them to
DynamoDB, AgentCore, Scheduler, SSM, S3, EventBridge and CloudWatch through module11/aws; the
tests and the simulator use fakes). It writes the item with a conditional put on `revision`
first (so `canaryPct=0` is live before the retarget), appends the audit event, performs the
actions, and polls the endpoint every 5 s for up to 40 s in the same invocation. Ledger puts
come last and never block a transition.
"""
from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from . import audit, latch
from . import signals as sig
from .router_spec import PROFILES, ROLLOUT_ID, TERMINAL_STATES, opening

POLL_INTERVAL = 5.0
POLL_WINDOW = 40.0
MAX_REISSUES = 3
STUCK_SECONDS = 600.0
SHADOW_PAIRS_MAX = 500
SHADOW_PAIRS_MIN = 200
SHADOW_RETRY_SECONDS = 7200.0
SHADOW_MARGIN = 0.05
SHADOW_P = 0.05
CANDIDATE_CLEARED = json.dumps({"state": "none"})
BATCH_NAME = r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$"

COMMANDS = ("open", "tick", "shadow-evaluate", "shadow-pass", "shadow-fail", "abort", "reset", "endpoints")


class InvalidTransition(ValueError):
    """The command is not valid from the item's state (`invalid_transition`)."""


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict = field(default_factory=dict)


@dataclass
class Decision:
    item: dict
    actions: list[Action] = field(default_factory=list)
    transitions: list[tuple[str, str, str]] = field(default_factory=list)   # (from, to, reason)
    note: str = ""
    changed: bool = True

    def kinds(self) -> list[str]:
        return [a.kind for a in self.actions]


def batch_name(stagekey: str, rollout_id: str, arm: str, attempt: int) -> str:
    """m11_<stagekey>_shadow_<rollout hex16>_{c,s}<attempt>."""
    if not ROLLOUT_ID.match(rollout_id):
        raise ValueError("rolloutId must be r-<16 lowercase hex>")
    if arm not in ("c", "s") or not 1 <= attempt <= 99:
        raise ValueError("arm must be c or s and attempt 1-99")
    return f"m11_{stagekey}_shadow_{rollout_id[2:]}_{arm}{attempt}"


def promoted_manifest(candidate_manifest: Mapping[str, Any], stage: str, version: int) -> dict:
    """/current after PROMOTED: what is now live, so drift stays clean."""
    doc = copy.deepcopy(dict(candidate_manifest))
    deployment = doc.setdefault("deployment", {}).setdefault(stage, {})
    deployment["endpoints"] = {"stable": version, "candidate": version}
    return doc


# ---------------------------------------------------------------- pure state machine
def _counters_reset(item: dict) -> None:
    item.update(breaches=0, cleanTicks=0, observingSince=None, insufficientSince={}, holdSince=None,
                stalled=[], lastCountedAt=None)


def _transition(d: Decision, to: str, reason: str = "") -> None:
    d.transitions.append((d.item["state"], to, reason))
    d.item["state"] = to


def _stall(d: Decision, reason: str, now: float) -> None:
    item = d.item
    if reason in item.setdefault("stalled", []):
        return
    item["stalled"].append(reason)
    d.actions.append(Action("emit", {"name": "m11.rollout.stalled", "detail": {"reason": reason}}))
    d.actions.append(Action("alarm", {"name": "m11-rollout-stalled", "reason": reason}))


def _enter_rollback(d: Decision, reason: str, now: float, latched: bool = True) -> None:
    item = d.item
    _transition(d, "ROLLING_BACK", reason)
    item.update(canaryPct=0, shadowPct=0, reason=reason, latch=latched, rollbackIssuedAt=now,
                rollbackReissues=0, stuckAlarmed=False)
    d.actions.append(Action("retarget", {"qualifier": "candidate", "version": item["stableVersion"]}))
    d.actions.append(Action("poll", {"qualifier": "candidate", "version": item["stableVersion"]}))


def _endpoint_ready(obs: Mapping | None, version: int) -> bool:
    if not obs:
        return False
    target = obs.get("targetVersion")
    return obs.get("status") == "READY" and _int(obs.get("liveVersion")) == int(version) and \
        (target is None or _int(target) == int(version))


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decide(item: Mapping[str, Any] | None, command: str, args: Mapping[str, Any] | None, profile: Mapping | None,
           log: sig.Log | None, now: float, obs: Mapping[str, Any] | None = None) -> Decision:
    if command not in COMMANDS:
        raise InvalidTransition(f"unknown command {command!r}")
    args = dict(args or {})
    current = copy.deepcopy(dict(item)) if item else {"state": "IDLE", "revision": 0, "endpoints": {}}
    state = current.get("state", "IDLE")
    d = Decision(current)
    if command == "open":
        return _open(d, args, now)
    if command == "endpoints":
        if state not in ("IDLE", *TERMINAL_STATES):
            raise InvalidTransition(f"endpoints is valid from IDLE and terminal states, not {state}")
        entries = current.setdefault("endpoints", {})
        for qualifier, entry in args.items():
            entries[qualifier] = {k: entry[k] for k in ("version", "manifestVersion", "behaviorDigest", "release")}
        current["endpointsWrittenAt"] = now
        return d
    if command == "reset":
        if state not in TERMINAL_STATES:
            raise InvalidTransition(f"reset is valid from PROMOTED or ROLLED_BACK, not {state}")
        _transition(d, "IDLE", args.get("reason", "reset"))
        current.update(canaryPct=0, shadowPct=0)
        return d
    if state in ("IDLE", *TERMINAL_STATES):
        if command == "tick":
            d.changed, d.note = False, f"no active rollout ({state})"
            return d
        raise InvalidTransition(f"{command} is not valid from {state}")
    profile = profile or PROFILES[current["profile"]]
    if command == "abort":
        _enter_rollback(d, args.get("reason", "abort"), now, latched=bool(args.get("latch", True)))
        return d
    if command in ("shadow-evaluate", "shadow-pass", "shadow-fail"):
        if state != "SHADOWING":
            raise InvalidTransition(f"{command} is valid from SHADOWING, not {state}")
        if command == "shadow-evaluate":
            return _shadow(d, profile, now, obs, force=True)
        return _shadow_verdict(d, profile, now, command == "shadow-pass", args.get("reason", "shadow_regression"))
    # tick
    if state == "SHADOWING":
        return _shadow(d, profile, now, obs)
    if state == "ROLLING_BACK":
        return _rolling_back(d, now, obs)
    if state == "ADVANCING":
        return _promoting(d, now, obs)
    if state == "WARMING":
        since = current.get("warmingSince", current["openedAt"])
        if log.candidate_count(since, now) < profile["warmupSessions"]:
            d.note = "warming"
            return d
        _transition(d, "OBSERVING", "warm-up complete")
        # The same tick then evaluates OBSERVING's rules.
    return _observe(d, profile, log, now)


def _open(d: Decision, args: Mapping[str, Any], now: float) -> Decision:
    item = d.item
    if item.get("state", "IDLE") not in ("IDLE", *TERMINAL_STATES):
        raise InvalidTransition(f"open is valid from IDLE, PROMOTED or ROLLED_BACK, not {item['state']}")
    for key in ("rolloutId", "profile", "stableVersion", "candidateVersion", "behaviorDigest", "manifestVersion",
                "release"):
        if args.get(key) in (None, ""):
            raise InvalidTransition(f"open needs {key}")
    if not ROLLOUT_ID.match(args["rolloutId"]):
        raise InvalidTransition("rolloutId must be r-<16 lowercase hex>")
    profile = PROFILES.get(args["profile"])
    if profile is None:
        raise InvalidTransition(f"unknown profile {args['profile']!r}")
    if args.get("experiment") and "shadowPhase" not in profile:
        raise InvalidTransition("experiment is allowed only for profiles with a shadow phase")
    stable = (item.get("endpoints") or {}).get("stable")
    if stable is not None and int(stable["version"]) != int(args["stableVersion"]):
        raise InvalidTransition(f"invalid_transition: stable endpoint serves v{stable['version']}, "
                                f"open names v{args['stableVersion']}")
    start = opening(args["profile"], args.get("shadowPct"))
    revision = int(item.get("revision", 0))
    endpoints = dict(item.get("endpoints") or {})
    endpoints["candidate"] = {"version": int(args["candidateVersion"]), "manifestVersion": args["manifestVersion"],
                              "behaviorDigest": args["behaviorDigest"], "release": args["release"]}
    new = {"pk": "ROLLOUT", "sk": "CURRENT", "rolloutId": args["rolloutId"], "profile": args["profile"],
           "state": item.get("state", "IDLE"), "canaryPct": start["canaryPct"], "shadowPct": start["shadowPct"],
           "stableVersion": int(args["stableVersion"]), "candidateVersion": int(args["candidateVersion"]),
           "behaviorDigest": args["behaviorDigest"], "manifestVersion": args["manifestVersion"],
           "release": args["release"], "experiment": bool(args.get("experiment")), "endpoints": endpoints,
           "revision": revision, "openedAt": now, "stepIndex": 0, "stepStarts": [now],
           "rateBreaches": 0, "looks": 0, "lastLookCandidates": 0, "lookedStep": None, "lastLookBreached": False,
           "rateExhausted": False, "reason": None, "latch": False, "shadow": {}, "endpointsWrittenAt": now}
    _counters_reset(new)
    if start["state"] == "WARMING":
        new["warmingSince"] = now
    d.item = new
    _transition(d, start["state"], "open")
    d.actions.append(Action("schedule", {"enabled": True}))
    return d


def _observe(d: Decision, profile: Mapping, log: sig.Log, now: float) -> Decision:
    item = d.item
    step = int(item["stepIndex"])
    step_start = float(item["stepStarts"][step])
    win = profile["window"]
    min_n = int(win["minSamplesPerArm"])
    alpha = float(profile["alpha"])
    n_c_step = log.count("candidate", step, step_start, now)
    n_s_step = log.count("stable", step, step_start, now)
    if item.get("observingSince") is None and n_c_step >= min_n and n_s_step >= min_n:
        item["observingSince"] = now
    marker = item.get("lastCountedAt")
    marker = step_start if marker is None or marker < step_start else float(marker)

    if win["mode"] == "sliding":
        t_from = max(step_start, now - float(win["lookbackSeconds"]))
        for arm in sig.ARMS:
            kth = log.kth_latest_end(arm, step, min_n, now)
            if kth is not None and kth <= t_from:
                t_from = max(step_start, kth - 1e-6)
        n_c, n_s = log.count("candidate", step, t_from, now), log.count("stable", step, t_from, now)
        new_min = int(win["minNewSamplesPerArm"])
        counted = (n_c >= min_n and n_s >= min_n and log.count("candidate", step, marker, now) >= new_min
                   and log.count("stable", step, marker, now) >= new_min)
    else:
        t_from = marker
        n_c, n_s = log.count("candidate", step, t_from, now), log.count("stable", step, t_from, now)
        counted = n_c >= min_n and n_s >= min_n

    results: dict[str, sig.SignalResult] = {}
    if counted:
        c, s = log.agg("candidate", step, t_from, now), log.agg("stable", step, t_from, now)
        if win["mode"] == "sliding":
            results["error_rate"] = sig.rate_window("error_rate", c, s, alpha, profile["minCandidateEvents"])
            results["tool_failure"] = sig.rate_window("tool_failure", c, s, alpha, profile["minCandidateEvents"])
        results["tool_selection"] = sig.tool_selection(c, s, alpha, min_n)
        # Candidate warm-up (router_spec.CANDIDATE_WARMUP_SECONDS): latency and cost use only sessions that end
        # that long after the step's first candidate session; until then they are insufficient.
        first = log.first_end("candidate", step)
        warm_from = None if first is None else first + float(profile.get("candidateWarmupSeconds", 0))
        if warm_from is None or warm_from > now:
            results["latency"] = sig.SignalResult("latency", sig.INSUFFICIENT, "candidate warm-up")
            results["cost"] = sig.SignalResult("cost", sig.INSUFFICIENT, "candidate warm-up")
        else:
            if warm_from > t_from:     # windows are (t_from, now]; the session ending at warm_from counts
                c, s = (log.agg("candidate", step, warm_from - 1e-6, now),
                        log.agg("stable", step, warm_from - 1e-6, now))
            results["latency"] = sig.latency(c, s, alpha, min_n)
            results["cost"] = sig.cost(c, s, alpha, min_n)
    q = sig.quality(log.quality("candidate", now), log.quality("stable", now), alpha)
    if counted:
        results["quality"] = q

    # The pooled rate rule (batch profiles).
    rule = profile.get("rateRule")
    rate_reason = None
    rate_breached_now = False
    if rule:
        cand_total = log.candidate_count(float(item["openedAt"]) - 1e-6, now)
        due = (n_c_step >= min_n and n_s_step >= min_n
               and cand_total - int(item.get("lastLookCandidates", 0)) >= int(rule["minNewCandidatePerLook"]))
        if due and int(item.get("looks", 0)) >= int(rule["maxLooks"]):
            item["rateExhausted"] = True
            _stall(d, "rate_looks_exhausted", now)
        elif due:
            item["looks"] = int(item.get("looks", 0)) + 1
            item["lastLookCandidates"] = cand_total
            item["lookedStep"] = step
            strata = log.strata(now)
            alpha_look = float(rule["alpha"]) / int(rule["maxLooks"])
            breached = [name for name in rule["signals"]
                        if sig.pooled_rate(name, strata[name], alpha_look, profile["minCandidateEvents"]).status
                        == sig.BREACH]
            if breached:
                item["rateBreaches"] = int(item.get("rateBreaches", 0)) + 1
                item["cleanTicks"] = 0
                item["lastLookBreached"] = True
                rate_reason = breached[0]
                rate_breached_now = True
            else:
                item["rateBreaches"] = 0
                item["lastLookBreached"] = False

    def sufficient(name: str) -> bool:
        if rule and name in rule["signals"]:
            return item.get("lookedStep") == step
        res = results.get(name)
        return res is not None and res.status != sig.INSUFFICIENT

    breach = [name for name in sig.SIGNAL_ORDER if name in results and results[name].status == sig.BREACH]
    required = list(profile["requiredSignals"])
    if counted and breach:
        item["breaches"] = int(item.get("breaches", 0)) + 1
        item["cleanTicks"] = 0
        item["lastCountedAt"] = now
    elif counted and not rate_breached_now and all(sufficient(name) for name in required):
        item["cleanTicks"] = int(item.get("cleanTicks", 0)) + 1
        item["breaches"] = 0
        item["lastCountedAt"] = now
    item["lastSignals"] = {k: v.status for k, v in results.items()}

    insufficient_since = item.setdefault("insufficientSince", {})
    for name in required:
        if counted and sufficient(name):
            insufficient_since.pop(name, None)
        else:
            insufficient_since.setdefault(name, now)
            if now - float(insufficient_since[name]) >= float(profile["stallSeconds"]):
                _stall(d, f"insufficient:{name}", now)

    if int(item.get("breaches", 0)) >= int(profile["consecutiveBreaches"]):
        _enter_rollback(d, breach[0] if breach else "breach", now)
        return d
    if rule and int(item.get("rateBreaches", 0)) >= int(rule["consecutive"]):
        _enter_rollback(d, rate_reason or "error_rate", now)
        return d
    observing_since = item.get("observingSince")
    if (int(item.get("cleanTicks", 0)) >= int(profile["cleanTicksToAdvance"]) and observing_since is not None
            and now - float(observing_since) >= float(profile["dwellSeconds"])
            and not item.get("lastLookBreached") and not item.get("rateExhausted")):
        steps = profile["steps"]
        to_last = step + 1 == len(steps) - 1
        if to_last and profile.get("qualityToPromote") and q.status != sig.OK:
            if item.get("holdSince") is None:
                item["holdSince"] = now
            if now - float(item["holdSince"]) >= float(profile["stallSeconds"]):
                _stall(d, "quality_hold", now)
            d.note = f"holding at {item['canaryPct']}%: quality {q.status}"
            return d
        _transition(d, "ADVANCING", f"step {steps[step]}% clean")
        if to_last:
            item["promoteIssuedAt"] = now
            d.actions.append(Action("retarget", {"qualifier": "stable", "version": item["candidateVersion"]}))
            d.actions.append(Action("poll", {"qualifier": "stable", "version": item["candidateVersion"]}))
            return d
        item["stepIndex"] = step + 1
        item["canaryPct"] = steps[step + 1]
        item["stepStarts"] = list(item["stepStarts"]) + [now]
        _counters_reset(item)
        _transition(d, "OBSERVING", f"step {steps[step + 1]}%")
    return d


def _rolling_back(d: Decision, now: float, obs: Mapping | None) -> Decision:
    item = d.item
    endpoint = (obs or {}).get("endpoints", {}).get("candidate")
    if _endpoint_ready(endpoint, item["stableVersion"]):
        _transition(d, "ROLLED_BACK", item.get("reason") or "")
        item["endpoints"] = dict(item.get("endpoints") or {})
        if "stable" in item["endpoints"]:
            item["endpoints"]["candidate"] = dict(item["endpoints"]["stable"])
        item["rolledBackAt"] = item["endpointsWrittenAt"] = now
        if item.get("latch"):
            d.actions.append(Action("latch", {"behaviorDigest": item["behaviorDigest"],
                                              "reason": item.get("reason") or "rollback"}))
        d.actions += [Action("clear_candidate"), Action("schedule", {"enabled": False}),
                      Action("emit", {"name": "m11.rollout.rolled_back", "detail": {"reason": item.get("reason")}})]
        return d
    if endpoint and endpoint.get("status") == "UPDATE_FAILED" and int(item.get("rollbackReissues", 0)) < MAX_REISSUES:
        item["rollbackReissues"] = int(item.get("rollbackReissues", 0)) + 1
        d.actions.append(Action("retarget", {"qualifier": "candidate", "version": item["stableVersion"]}))
    if now - float(item.get("rollbackIssuedAt") or now) >= STUCK_SECONDS and not item.get("stuckAlarmed"):
        item["stuckAlarmed"] = True
        d.actions.append(Action("alarm", {"name": "m11-rollback-stuck"}))
    d.actions.append(Action("poll", {"qualifier": "candidate", "version": item["stableVersion"]}))
    d.changed = bool(d.actions[:-1])
    return d


def _promoting(d: Decision, now: float, obs: Mapping | None) -> Decision:
    item = d.item
    endpoint = (obs or {}).get("endpoints", {}).get("stable")
    if _endpoint_ready(endpoint, item["candidateVersion"]):
        _transition(d, "PROMOTED", "promoted")
        item["canaryPct"] = 0
        item["endpoints"] = dict(item.get("endpoints") or {})
        if "candidate" in item["endpoints"]:
            item["endpoints"]["stable"] = dict(item["endpoints"]["candidate"])
        item["promotedAt"] = item["endpointsWrittenAt"] = now
        d.actions += [Action("write_current", {"version": item["candidateVersion"]}), Action("clear_candidate"),
                      Action("schedule", {"enabled": False}),
                      Action("emit", {"name": "m11.rollout.promoted", "detail": {}})]
        return d
    if endpoint and endpoint.get("status") == "UPDATE_FAILED" and int(item.get("promoteReissues", 0)) < MAX_REISSUES:
        item["promoteReissues"] = int(item.get("promoteReissues", 0)) + 1
        d.actions.append(Action("retarget", {"qualifier": "stable", "version": item["candidateVersion"]}))
    d.actions.append(Action("poll", {"qualifier": "stable", "version": item["candidateVersion"]}))
    d.changed = bool(d.actions[:-1])
    return d


# ---------------------------------------------------------------- shadow phase
def _shadow(d: Decision, profile: Mapping, now: float, obs: Mapping | None, force: bool = False) -> Decision:
    item = d.item
    phase = profile.get("shadowPhase") or {}
    shadow = item.setdefault("shadow", {})
    obs = obs or {}
    if "shadowPairs" in obs:
        shadow["pairCount"] = len(obs["shadowPairs"])
    elapsed = now - float(item["openedAt"])
    if elapsed >= float(phase.get("maxHours", 0)) * 3600 and phase:
        if item.get("experiment") and shadow.get("verdict"):
            _enter_rollback(d, "experiment_ended", now, latched=False)
        else:
            _enter_rollback(d, "shadow_timeout", now, latched=False)
            d.actions.append(Action("emit", {"name": "m11.rollout.shadow_timeout", "detail": {}}))
        return d
    if shadow.get("verdict") and item.get("experiment"):
        d.note = "experiment: verdict recorded"
        return d
    running = bool(shadow.get("running"))
    if force and (running or shadow.get("verdictStartedAt")):
        d.changed, d.note = False, "verdict already started"
        return d
    if not running:
        due = force or elapsed >= float(phase.get("minHours", 0)) * 3600
        if not due or (shadow.get("nextAttemptAt") and now < float(shadow["nextAttemptAt"]) and not force):
            d.note = "shadowing"
            return d
        pairs = list(obs.get("shadowPairs") or [])
        attempt = int(shadow.get("attempt", 0)) + 1
        if len(pairs) > SHADOW_PAIRS_MAX:
            pairs = random.Random(f"{item['rolloutId']}:{attempt}").sample(pairs, SHADOW_PAIRS_MAX)
        stagekey = obs.get("stagekey", "prod")
        names = {arm: batch_name(stagekey, item["rolloutId"], arm, attempt) for arm in ("c", "s")}
        shadow.update(attempt=attempt, verdictStartedAt=shadow.get("verdictStartedAt") or now, batchNames=names,
                      batchIds={}, attemptStartedAt=now, nextAttemptAt=None, running=True)
        d.actions.append(Action("start_batch", {"names": names,
                                                "sessionIds": {"c": [p[0] for p in pairs], "s": [p[1] for p in pairs]}}))
        d.note = f"shadow verdict attempt {attempt} started with {len(pairs)} pairs"
        return d
    batches = obs.get("batches") or {}
    statuses = {arm: (batches.get(arm) or {}).get("status") for arm in ("c", "s")}
    if any(st == "FAILED" for st in statuses.values()):
        return _shadow_retry(d, now, "batch FAILED")
    if not all(st in ("COMPLETED", "COMPLETED_WITH_ERRORS") for st in statuses.values()):
        d.note = "shadow verdict running"
        d.changed = False
        return d
    deltas = obs.get("shadowDeltas") or {}
    scored = min((len(v) for v in deltas.values()), default=0)
    if scored < SHADOW_PAIRS_MIN:
        return _shadow_retry(d, now, f"{scored} scored pairs")
    from .stats import mean, paired_less
    failed = [ev for ev, values in sorted(deltas.items())
              if mean(values) < -SHADOW_MARGIN and paired_less(values) < SHADOW_P]
    shadow["scoredPairs"] = scored
    return _shadow_verdict(d, profile, now, not failed, "shadow_regression", internal=True)


def _shadow_retry(d: Decision, now: float, why: str) -> Decision:
    shadow = d.item["shadow"]
    shadow.update(running=False, nextAttemptAt=now + SHADOW_RETRY_SECONDS, lastFailure=why)
    d.note = f"shadow attempt {shadow.get('attempt')} unusable ({why}); next attempt in 2 h"
    return d


def _shadow_verdict(d: Decision, profile: Mapping, now: float, passed: bool, reason: str,
                    internal: bool = False) -> Decision:
    item = d.item
    shadow = item.setdefault("shadow", {})
    shadow["verdict"] = "pass" if passed else "fail"
    shadow["running"] = False
    if item.get("experiment"):
        d.actions.append(Action("audit", {"action": "shadow_verdict", "decision": shadow["verdict"]}))
        d.note = f"experiment verdict {shadow['verdict']}; staying in SHADOWING"
        return d
    if passed:
        _transition(d, "WARMING", "shadow-pass")
        item.update(canaryPct=profile["steps"][0], warmingSince=now, stepIndex=0, stepStarts=[now])
        _counters_reset(item)
        return d
    _enter_rollback(d, reason, now, latched=True)
    return d


# ---------------------------------------------------------------- orchestration
class ConditionalCheckFailed(Exception):
    pass


class Ports(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
    def get_item(self) -> dict | None: ...
    def put_item(self, item: dict, expected_revision: int | None) -> None: ...
    def put_audit(self, event: dict) -> None: ...
    def log_for(self, item: dict) -> sig.Log: ...
    def shadow_obs(self, item: dict) -> dict: ...
    def get_endpoint(self, qualifier: str) -> dict: ...
    def update_endpoint(self, qualifier: str, version: int) -> None: ...
    def get_schedule(self) -> dict: ...
    def update_schedule(self, **fields: Any) -> None: ...
    def get_param(self, name: str) -> str | None: ...
    def put_param(self, name: str, value: str) -> None: ...
    def put_ledger(self, key: str, body: bytes) -> None: ...
    def put_metric(self, namespace: str, name: str, value: float) -> None: ...
    def emit(self, name: str, detail: dict) -> None: ...
    def alarm(self, name: str, reason: str | None = None) -> None: ...
    def start_batch(self, name: str, session_ids: list[str]) -> str: ...
    def log(self, level: str, event: str, **fields: Any) -> None: ...


SCHEDULE_FIELDS = ("Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone", "StartDate", "EndDate",
                   "Description", "Target", "FlexibleTimeWindow", "KmsKeyArn", "ActionAfterCompletion")


class Controller:
    """Runs decisions against the ports. One instance per Lambda invocation."""

    def __init__(self, ports: Ports, stage: str, current_param: str, candidate_param: str,
                 rng: random.Random | None = None) -> None:
        self.p, self.stage = ports, stage
        self.current_param, self.candidate_param = current_param, candidate_param
        self.rng = rng or random.Random()

    def handle(self, event: Mapping[str, Any]) -> dict:
        command, args = event.get("command"), event.get("args") or {}
        expected = event.get("expectedRevision")
        for attempt in (1, 2):
            item = self.p.get_item()
            if expected is not None and item is not None and int(item.get("revision", 0)) != int(expected):
                return {"ok": False, "error": "revision_mismatch", "revision": item.get("revision")}
            try:
                decision = self._decide(item, command, args)
            except InvalidTransition as exc:
                return {"ok": False, "error": "invalid_transition", "detail": str(exc)}
            try:
                item = self._commit(item, decision)
            except ConditionalCheckFailed:
                if attempt == 2:
                    self.p.log("WARN", "conditional_check_failed", command=command)
                    return {"ok": False, "error": "conflict"}
                continue
            item = self._run_actions(item, decision)
            return {"ok": True, "state": item.get("state"), "revision": item.get("revision"), "note": decision.note}
        return {"ok": False, "error": "conflict"}  # pragma: no cover

    # -- helpers
    def _decide(self, item: dict | None, command: str, args: Mapping, obs: Mapping | None = None) -> Decision:
        now = self.p.now()
        state = (item or {}).get("state", "IDLE")
        profile = PROFILES.get((item or {}).get("profile", "")) if item else None
        log = self.p.log_for(item) if command == "tick" and state in ("WARMING", "OBSERVING") else None
        if obs is None:
            obs = {}
            if command in ("tick", "shadow-evaluate") and state == "SHADOWING":
                obs = {**self.p.shadow_obs(item), "stagekey": self.stage}
            elif command == "tick" and state == "ROLLING_BACK":
                obs = {"endpoints": {"candidate": self.p.get_endpoint("candidate")}}
            elif command == "tick" and state == "ADVANCING":
                obs = {"endpoints": {"stable": self.p.get_endpoint("stable")}}
        return decide(item, command, args, profile, log, now, obs)

    def _commit(self, before: dict | None, d: Decision) -> dict:
        if not d.changed and not d.transitions:
            return d.item
        expected = int(before.get("revision", 0)) if before else None
        d.item["revision"] = (expected or 0) + 1
        d.item["updatedAt"] = audit.iso(self.p.now())
        self.p.put_item(d.item, expected)
        for frm, to, reason in d.transitions:
            self.p.put_audit(audit.audit_event(d.item["rolloutId"], self.p.now(), "transition",
                                               decision=f"{frm}->{to}", details={"reason": reason}))
        return d.item

    def _run_actions(self, item: dict, d: Decision) -> dict:
        ledger: list[dict] = []
        poll = None
        for action in d.actions:
            kind, a = action.kind, action.args
            if kind == "retarget":
                self.p.update_endpoint(a["qualifier"], int(a["version"]))
            elif kind == "poll":
                poll = a
            elif kind == "schedule":
                self._schedule(a["enabled"])
            elif kind == "write_current":
                manifest = json.loads(self.p.get_param(self.candidate_param) or "{}")
                self.p.put_param(self.current_param, json.dumps(promoted_manifest(manifest, self.stage, a["version"]),
                                                                sort_keys=True))
            elif kind == "clear_candidate":
                self.p.put_param(self.candidate_param, CANDIDATE_CLEARED)
            elif kind == "latch":
                name = latch.name(a["behaviorDigest"])
                self.p.put_param(name, latch.value(a["behaviorDigest"], item["rolloutId"], a["reason"], self.p.now()))
                ledger.append(audit.ledger_entry("latch", self.stage, self.p.now(), "controller", parameter=name,
                                                 behaviorDigest=a["behaviorDigest"], rolloutId=item["rolloutId"]))
            elif kind == "emit":
                self.p.emit(a["name"], {**a.get("detail", {}), "rolloutId": item.get("rolloutId"), "stage": self.stage})
            elif kind == "alarm":
                self.p.alarm(a["name"], a.get("reason"))
            elif kind == "audit":
                self.p.put_audit(audit.audit_event(item["rolloutId"], self.p.now(), a["action"],
                                                   decision=a.get("decision")))
            elif kind == "start_batch":
                ids = {}
                for arm, name in a["names"].items():
                    ids[arm] = self.p.start_batch(name, a["sessionIds"][arm])
                    ledger.append(audit.ledger_entry("batch-evaluation", self.stage, self.p.now(), "controller",
                                                     name=name, batchEvaluationId=ids[arm],
                                                     rolloutId=item["rolloutId"]))
                item = copy.deepcopy(item)
                item["shadow"]["batchIds"] = ids
                item = self._commit(item, Decision(item, transitions=[]))
        if poll is not None:
            item = self._poll(item, poll)
        # Ledger entries last: after the conditional write and the SSM write.
        for entry in ledger:
            audit.put_with_retry(self.p.put_ledger, audit.ledger_key(self.stage, self.p.now()), entry,
                                 sleep=self.p.sleep, log=self.p.log, metric=self.p.put_metric, rng=self.rng)
        return item

    def _poll(self, item: dict, poll: Mapping) -> dict:
        waited = 0.0
        while waited + POLL_INTERVAL <= POLL_WINDOW + 1e-9:
            self.p.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL
            obs = {"endpoints": {poll["qualifier"]: self.p.get_endpoint(poll["qualifier"])}}
            d = decide(item, "tick", {}, PROFILES[item["profile"]], None, self.p.now(), obs)
            if d.transitions or any(a.kind == "retarget" for a in d.actions) or \
                    any(a.kind == "alarm" for a in d.actions):
                d.actions = [a for a in d.actions if a.kind != "poll"]
                item = self._commit(item, d)
                item = self._run_actions(item, d)
                if d.transitions:
                    return item
        return item

    def _schedule(self, enabled: bool) -> None:
        """UpdateSchedule replaces the whole definition: copy every field from GetSchedule (N6)."""
        current = self.p.get_schedule()
        fields = {k: current[k] for k in SCHEDULE_FIELDS if current.get(k) is not None}
        fields["State"] = "ENABLED" if enabled else "DISABLED"
        self.p.update_schedule(**fields)
