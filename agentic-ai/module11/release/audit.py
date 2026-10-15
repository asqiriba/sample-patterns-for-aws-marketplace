# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Audit events and release-ledger entries. Pure helpers.

- Audit events go to the control table as {pk: ROLLOUT#<id>, sk: EVENT#<ts>}, with Module 8's
  audit field names (actor, action, decision, details).
- The release ledger is one S3 object per entry, `ledger/<stage>/<ts>-<uuid4>.json`, with a
  fixed-width UTC timestamp so a prefix listing returns entries in time order. Nothing ever
  rewrites or deletes an entry. Stage-less writers use `ledger/shared/`.
- A failed ledger put is retried 3 times with jittered backoff (1, 2, 4 s), then logged as
  `ledger_write_failed` at ERROR with the full entry (IDs only) and counted in the metric
  m11/LedgerWriteFailures. It never blocks or reorders a transition.
"""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

LEDGER_RETRY_DELAYS = (1.0, 2.0, 4.0)
LEDGER_FAILURE_METRIC = ("m11", "LedgerWriteFailures")
SHARED = "shared"


def stamp(epoch_seconds: float) -> str:
    """Fixed-width UTC timestamp: 20261020T140211.123456Z."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def audit_event(rollout_id: str, at: float, action: str, *, actor: str = "controller",
                decision: str | None = None, details: Mapping[str, Any] | None = None) -> dict:
    return {"pk": f"ROLLOUT#{rollout_id}", "sk": f"EVENT#{stamp(at)}", "at": iso(at), "actor": actor,
            "action": action, "decision": decision, "details": dict(details or {})}


def ledger_key(stage: str, at: float, unique: str | None = None) -> str:
    return f"ledger/{stage}/{stamp(at)}-{unique or uuid.uuid4()}.json"


def ledger_entry(kind: str, stage: str, at: float, by: str, **fields: Any) -> dict:
    return {"kind": kind, "stage": stage, "at": iso(at), "by": by, **fields}


def put_with_retry(put: Callable[[str, bytes], None], key: str, entry: Mapping[str, Any], *,
                   sleep: Callable[[float], None], log: Callable[..., None],
                   metric: Callable[[str, str, float], None], rng: random.Random | None = None) -> bool:
    """Put one ledger entry: 1 attempt + 3 retries. Returns False after `ledger_write_failed`."""
    body = json.dumps(entry, sort_keys=True).encode("utf-8")
    rng = rng or random.Random()
    for attempt, delay in enumerate((*LEDGER_RETRY_DELAYS, None)):
        try:
            put(key, body)
            return True
        except Exception as exc:  # noqa: BLE001 - any put failure is retried, then reported
            if delay is None:
                log("ERROR", "ledger_write_failed", key=key, entry=dict(entry), error=type(exc).__name__,
                    attempts=attempt + 1)
                metric(*LEDGER_FAILURE_METRIC, 1.0)
                return False
            sleep(delay * rng.uniform(0.75, 1.25))
    return False  # pragma: no cover


def sort_entries(keys: list[str]) -> list[str]:
    """Ledger keys in the order a ListObjectsV2 prefix listing returns them (lexicographic)."""
    return sorted(keys)
