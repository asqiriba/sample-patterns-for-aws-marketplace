# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 demo arm --stage dev [--rearm] [--reason]`: rehearse the B5 canary on dev.

It deploys the deliberately bad payments manifest (`demo/wording.py: PAYMENTS_TWEAK`) to dev with the operator
`release deploy`, which opens a demo-profile rollout, and records `demo-state.devArmed`. Guards, all before any
write: `--stage` is argparse `choices=["dev"]` (prod and staging exit 2 before any client exists); dev's
rolloutProfile is `demo`; the caller is an operator principal; `module11/.local/locks/dev.lock` is absent (a
stale pid is reported, never removed); the dev control item is IDLE, ROLLED_BACK or PROMOTED (a non-terminal
rollout, or a latch on the bad digest, needs --rearm); dev /current is not already the bad release.

--rearm (dev only): stop a dev traffic driver (SIGTERM/SIGKILL/killedAt), abort a non-terminal rollout, latch
clear (audited), flags restore, purge (h.4 purge-wait rule) and reset, each idempotent; any failure exits 4.
Then a `demo_arm` audit record (exit 5 and no deploy if it fails), then the deploy (exit 6 on a deploy error).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from module11.demo import locks as L
from module11.demo import rearm as R
from module11.demo import state as S

TERMINAL_OR_IDLE = ("IDLE", "ROLLED_BACK", "PROMOTED")


@dataclass
class ArmPorts(R.Ports):
    """rearm.Ports plus the dev-only reads and the deploy."""
    lock_dir: Path | None = None
    lock_alive: Callable[[int], bool] = L.pid_alive
    bad_digest: Callable[[], str] = lambda: ""                  # behavior digest of the bad payments manifest
    current_digest: Callable[[], str | None] = lambda: None     # dev /current's behavior digest
    latched: Callable[[str], bool] = lambda digest: False
    deploy_bad: Callable[[], Mapping] | None = None             # -> the opened control item (rolloutId, openedAt)


def arm(ports: ArmPorts, *, rearm: bool = False, reason: str = "demo dry run") -> int:
    try:
        return _arm(ports, rearm, reason)
    except R.Exit as exc:
        R._say(ports, exc.message, exc.level)
        return exc.code


def _arm(ports: ArmPorts, rearm: bool, reason: str) -> int:
    if ports.stage != "dev":                       # argparse already refuses; defense in depth
        raise R.Exit(2, "demo arm acts on dev only")
    if ports.rollout_profile() != "demo":
        raise R.Exit(3, "demo arm refuses outside the demo rollout profile")
    if not R._is_operator(ports.caller_arn(), ports.operator_principals):
        raise R.Exit(3, "caller is not an operator principal")
    lock = L.read("dev", ports.lock_dir, ports.lock_alive)
    if lock is not None:
        raise R.Exit(3, lock.message())
    item = dict(ports.read_control() or {})
    state = item.get("state", "IDLE")
    bad = ports.bad_digest()
    if state not in TERMINAL_OR_IDLE and not rearm:
        raise R.Exit(3, "a rollout is active on dev; use --rearm")
    if ports.latched(bad) and not rearm:
        raise R.Exit(3, "latched on dev; use --rearm")
    if ports.current_digest() == bad:
        raise R.Exit(3, "dev already serves the bad release; restore dev's baseline (OPERATIONS.md)")
    prior = {"rolloutId": item.get("rolloutId"), "state": state}
    if rearm:
        _rearm_dev(ports, item, reason)
    try:
        record_id = ports.audit("demo_arm", {"stage": "dev", "behaviorDigest": bad, "reason": reason,
                                             "caller": ports.caller_arn(), "priorRolloutId": prior["rolloutId"],
                                             "priorState": prior["state"]})
    except Exception as exc:  # noqa: BLE001
        raise R.Exit(5, f"audit write failed ({type(exc).__name__}: {exc}); nothing deployed") from exc
    R._say(ports, f"audit record {record_id}")
    try:
        opened = ports.deploy_bad()
    except Exception as exc:  # noqa: BLE001
        raise R.Exit(6, f"deploy failed: {type(exc).__name__}: {exc}") from exc
    record = {"rolloutId": opened.get("rolloutId"), "digest": bad,
              "openedAt": R._iso(opened.get("openedAt") or ports.clock()), "auditRecord": record_id}
    R._update(ports, lambda doc: {**doc, "devArmed": record}, "devArmed")
    R._say(ports, f"dev armed: rollout {record['rolloutId']} for the bad payments manifest (audit {record_id})")
    return 0


def _rearm_dev(ports: ArmPorts, item: Mapping, reason: str) -> None:
    """Step 2: driver stop, abort, latch clear, flags restore, purge, reset (each idempotent; failure -> exit 4)."""
    rid = item.get("rolloutId")
    state = item.get("state", "IDLE")
    if not rid:
        return
    try:
        if state in R.NON_TERMINAL:
            terminal = R.settle(ports, item, rid, reason)
        else:
            R.stop_driver(ports, rid)
            terminal = item
        if state == "IDLE" and not (terminal.get("rolledBackAt") or terminal.get("promotedAt")):
            return
        R.cleanup(ports, terminal, reason)
    except R.Exit as exc:
        if exc.code == 4:
            raise R.Exit(4, exc.message.replace("nothing retried; re-run the command",
                                                "nothing deployed; re-run with --rearm")) from exc
        raise


def dev_armed(path: Path | None = None) -> dict | None:
    return S.read(path).get("devArmed")
