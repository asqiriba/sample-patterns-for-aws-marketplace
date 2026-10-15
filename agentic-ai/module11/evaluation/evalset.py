# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The evaluator-set change procedure (the source's six steps).

`rebaseline(old, new, ...)` where `old` and `new` are two `evaluators.yaml` documents:

1. Set the freeze `planned` with reason `evalset <old>-<new>`. It refuses to start (`FreezeHeld`) while a freeze
   it does not own is active, such as a shadow experiment's, so step 6 can never end someone else's freeze.
2. Run the full suite at 5 repeats on prod `stable` under the new set (and, in the same sessions, the evaluators
   that only the old set has).
3. Score the same sessions under the old set and record per-category deltas: the mean the old set reports for a
   category against the mean the new set reports.
4. Require the threshold edit in the same change: `thresholds.yaml`'s `evaluatorSetVersion` must equal the new
   set's version and differ from the old one, else `EvalSetMismatch`. The check runs first, so a
   missing edit costs nothing and sets no freeze.
5. Write the new baseline, stamped with the new version, ONLY if no record exists yet at
   `baselines/<manifestVersion>/<new>/baseline.json`. A calibration written for the new set (8 runs at 3 repeats)
   is better evidence than this single 5-repeat run, so `evalset` never overwrites one; it then writes only the
   change record. `noise.json` comes only from `calibrate`.
6. Unfreeze, clearing only a freeze whose reason is its own. The previous set's baseline is kept for one release.

The change record goes to `harness/evaluation/evalset-changes/<from>-<to>.json`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from module11.release import stats

from . import calibrate as CAL
from .records import RunRecord, S3Store, baseline_prefix

REPEATS = 5
CHANGES_DIR = "module11/harness/evaluation/evalset-changes"


class FreezeHeld(RuntimeError):
    def __init__(self, freeze: Mapping[str, Any]) -> None:
        super().__init__(f"a freeze is active ({freeze.get('state')}: {freeze.get('reason')}, by {freeze.get('by')}); "
                         "evalset rebaseline will not touch it")
        self.freeze = dict(freeze)


class EvalSetMismatch(RuntimeError):
    pass


def reason_for(old: str, new: str) -> str:
    return f"evalset {old}-{new}"


def set_ids(doc: Mapping[str, Any]) -> list[str]:
    return [e["id"] for e in doc["evaluators"]]


def union(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict]:
    """The evaluator entries to run: every new-set entry, then the old-set entries the new set lacks."""
    seen, out = set(), []
    for entry in [*new["evaluators"], *old["evaluators"]]:
        if entry["id"] not in seen:
            seen.add(entry["id"])
            out.append(entry)
    return out


def deltas(record: Mapping[str, Any], old_ids: Sequence[str], new_ids: Sequence[str]) -> dict[str, dict]:
    """Per category: the mean the old set reports, the mean the new set reports, and the difference."""
    out: dict[str, dict] = {}
    for category in sorted({r["category"] for r in record["results"]}):
        def mean_of(ids: Sequence[str]) -> float | None:
            values = [float(r["value"]) for r in record["results"]
                      if r["category"] == category and r["evaluatorId"] in ids and r.get("value") is not None]
            return stats.mean(values) if values else None
        old_mean, new_mean = mean_of(old_ids), mean_of(new_ids)
        out[category] = {"old": old_mean, "new": new_mean,
                         "delta": None if old_mean is None or new_mean is None else new_mean - old_mean}
    return out


def rebaseline(old: Mapping[str, Any], new: Mapping[str, Any], *, thresholds: Mapping[str, Any],
               get_freeze: Callable[[], Mapping[str, Any]], set_freeze: Callable[[Mapping[str, Any]], None],
               run_suite: Callable[[list[dict]], RunRecord], store: S3Store, manifest_version: str, suite: str,
               scenarios: Sequence[Mapping[str, Any]], evaluators: Sequence[str], lambda_tools: Sequence[str],
               by: str, root: Path | None = None, log: Callable[[str], None] = print,
               clock: Callable[[], float] = time.time) -> dict:
    """Runs the six steps; returns the change record (also written under `root` when given).
    `run_suite(entries)` runs the full suite at `REPEATS` on prod stable with those evaluator entries."""
    old_v, new_v = str(old["version"]), str(new["version"])
    if new_v == old_v:
        raise EvalSetMismatch(f"evaluators.yaml version is {new_v} in both sets: bump it")
    if str(thresholds.get("evaluatorSetVersion")) != new_v:
        raise EvalSetMismatch(f"thresholds.yaml evaluatorSetVersion is {thresholds.get('evaluatorSetVersion')!r}, "
                              f"the new evaluator set is {new_v!r}: edit both in the same change")
    reason = reason_for(old_v, new_v)
    held = dict(get_freeze() or {"state": "none"})
    if held.get("state", "none") != "none":
        raise FreezeHeld(held)
    set_freeze({"state": "planned", "reason": reason, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))})
    log(f"freeze planned: {reason}")
    try:
        record = run_suite(union(old, new))
        doc = record.to_dict()
        if doc["manifestVersion"] != manifest_version:
            raise CAL.CalibrationError(f"prod moved to {doc['manifestVersion']} during the run (expected {manifest_version})")
        per_category = deltas(doc, set_ids(old), set_ids(new))
        key_base = baseline_prefix(manifest_version, new_v)
        baseline_key = f"{key_base}/baseline.json"
        written = False
        if store.exists(baseline_key):
            log(f"{baseline_key} exists (a calibration wrote it): kept, change record only")
        else:
            body, _ = CAL.compute([doc], scenarios, thresholds, evaluators, CAL.scope_names(scenarios, lambda_tools))
            store.put(baseline_key, {"schema": "m11.baseline/1", "manifestVersion": manifest_version,
                                     "evaluatorSetVersion": new_v, "suiteSha256": suite, "calibrationId": None,
                                     "source": f"evalset rebaseline run {record.runId}", "nRuns": 1, "rCal": REPEATS,
                                     "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
                                     "runIds": [record.runId], **body})
            written = True
        store.put(f"evaluations/{record.stage}/{record.runId}.json", doc)
        change = {"from": old_v, "to": new_v, "manifestVersion": manifest_version, "runId": record.runId,
                  "repeats": REPEATS, "added": sorted(set(set_ids(new)) - set(set_ids(old))),
                  "removed": sorted(set(set_ids(old)) - set(set_ids(new))), "categoryDeltas": per_category,
                  "baselineWritten": written, "baselineKey": baseline_key,
                  "note": "noise.json comes only from `module11 calibrate`" if written else "baseline kept from calibration"}
        if root is not None:
            path = Path(root) / CHANGES_DIR / f"{old_v}-{new_v}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(change, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            log(f"wrote {path.relative_to(root)}")
        return change
    finally:
        now = dict(get_freeze() or {})
        if now.get("state") == "planned" and now.get("reason") == reason:       # only our own freeze
            set_freeze({"state": "none"})
            log("freeze cleared")
