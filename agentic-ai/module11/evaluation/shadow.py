# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shadow-experiment reports and the model comparison. Pure.

- `shadow report <name|id>`: an experiment's record (`module11/.local/experiments/<id>.json`, written by
  `shadow experiment`), found by its ID or by the `--name` it was started with (`model-migration`), newest first:
  its source, stage, real duration (opened to verdict), scored pairs, verdict, batch-evaluation IDs and paired
  score deltas.
- `model compare`: the full suite on a dev candidate that serves a `model/<profile>` branch, compared per category
  with the calibrated baseline of the current model. Each (evaluator, category) row has the candidate mean, the
  baseline mean, the delta and whether it is beyond the calibrated noise (2 x noise_r). The change is classified:
  `broad` (instruction style) when two or more categories move beyond noise in the same direction, `narrow` (a
  tool description or formatting) when the movement is confined to one category, `none` otherwise. The record
  names the model IDs it compared, read from models.yaml and the experiment manifest, never typed.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

JUDGED = ("Builtin.GoalSuccessRate", "Builtin.Correctness")


def find(local_dir: Path, key: str) -> dict | None:
    """The experiment record with ID `key`, or the newest whose `name` is `key`."""
    path = Path(local_dir) / f"{key}.json"
    if path.is_file():
        return json.loads(path.read_text())
    named = []
    for p in Path(local_dir).glob("x-*.json"):
        try:
            doc = json.loads(p.read_text())
        except ValueError:
            continue
        if doc.get("name") == key:
            named.append(doc)
    return max(named, key=lambda d: float(d.get("startedAt") or 0)) if named else None


def _when(epoch: Any) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    hours, rem = divmod(int(round(seconds)), 3600)
    return f"{hours} h {rem // 60:02d} min"


def report_lines(doc: Mapping[str, Any]) -> list[str]:
    opened, verdict_at = doc.get("openedAt"), doc.get("verdictAt") or doc.get("finishedAt")
    real = float(verdict_at) - float(opened) if opened and verdict_at else None
    out = [f"shadow experiment {doc.get('id')}" + (f" ({doc['name']})" if doc.get("name") else "") +
           f": source {doc.get('source')} ({str(doc.get('sourceSha', ''))[:12]}) on {doc.get('stage')}",
           f"  ran {_when(opened)} to {_when(verdict_at)}: a shadow run of {duration_text(real)} "
           f"(planned {doc.get('hours')} h; the production recommendation for a model change is a day or more)",
           f"  candidate manifest {doc.get('manifestVersion')}, rollout {doc.get('rolloutId')}, every session shadowed, "
           "none routed to the candidate, mutating tools suppressed in the copies"]
    if doc.get("verdict"):
        out.append(f"  verdict {doc['verdict']} from {doc.get('scoredPairs')} scored pairs; batch evaluations "
                   f"{', '.join(str(b) for b in (doc.get('batchIds') or {}).values()) if isinstance(doc.get('batchIds'), dict) else doc.get('batchIds')}")
    else:
        out.append("  no verdict recorded yet")
    for name, delta in sorted((doc.get("deltas") or {}).items()):
        out.append(f"  paired delta {name}: {delta:+.3f}" if isinstance(delta, (int, float)) else f"  {name}: {delta}")
    if doc.get("finishedAt"):
        out.append(f"  finished {_when(doc['finishedAt'])}: prod back on /current, freeze none")
    return out


# ---------------------------------------------------------------- model compare
def compare_rows(results: Iterable[Mapping[str, Any]], baseline: Mapping[str, Any] | None,
                 noise: Mapping[str, Any] | None, *, repeats: int) -> list[dict]:
    """Per (evaluator, category): candidate mean vs the calibrated baseline (scope `full`)."""
    by: dict[tuple[str, str], list[float]] = {}
    for r in results:
        if r.get("value") is None or r.get("evaluatorId") not in JUDGED:
            continue
        by.setdefault((r["evaluatorId"], r["category"]), []).append(float(r["value"]))
    stats = (baseline or {}).get("stats") or {}
    sd = (noise or {}).get("sd") or {}
    r_cal = int((noise or {}).get("r_cal") or 3)
    rows = []
    for (ev, cat), values in sorted(by.items()):
        key = f"{ev}|{cat}|full"
        base = stats.get(key)
        mean = sum(values) / len(values)
        noise_r = float(sd[key]) * math.sqrt(r_cal / max(1, repeats)) if key in sd else None
        delta = mean - float(base[0]) if base else None
        rows.append({"evaluator": ev, "category": cat, "n": len(values), "candidate": round(mean, 4),
                     "baseline": round(float(base[0]), 4) if base else None,
                     "delta": round(delta, 4) if delta is not None else None,
                     "noise2": round(2 * noise_r, 4) if noise_r is not None else None,
                     "beyondNoise": bool(delta is not None and noise_r is not None and abs(delta) > 2 * noise_r)})
    return rows


def classify(rows: Sequence[Mapping[str, Any]]) -> str:
    moved = [r for r in rows if r["beyondNoise"]]
    if not moved:
        return "none"
    categories = {r["category"] for r in moved}
    signs = {math.copysign(1, r["delta"]) for r in moved}
    if len(categories) >= 2 and len(signs) == 1:
        return "broad"
    return "narrow" if len(categories) == 1 else "mixed"


CLASS_TEXT = {"broad": "broad (instruction style): revise the instructions on the branch, recalibrate, then shadow",
              "narrow": "narrow (a tool description or formatting): fix that artifact on the branch",
              "mixed": "mixed: categories move in different directions; read the scenarios below",
              "none": "no category moved beyond the calibrated noise"}


def compare_lines(record: Mapping[str, Any]) -> list[str]:
    out = [f"model compare {record.get('id')}: candidate {record.get('candidateModel')} vs current "
           f"{record.get('currentModel')} on the full suite ({record.get('sessions')} sessions, dev candidate, "
           f"branch {record.get('branch')})",
           f"  baseline: calibration of {record.get('baselineManifest')} (evaluator set {record.get('evaluatorSet')})"]
    out.append(f"  {'evaluator':<26} {'category':<14} {'candidate':>9} {'baseline':>9} {'delta':>8} {'2 x noise':>9}")
    for r in record.get("rows") or []:
        base = f"{r['baseline']:.3f}" if r["baseline"] is not None else "-"
        delta = f"{r['delta']:+.3f}" if r["delta"] is not None else "-"
        noise = f"{r['noise2']:.3f}" if r["noise2"] is not None else "-"
        out.append(f"  {r['evaluator']:<26} {r['category']:<14} {r['candidate']:>9.3f} {base:>9} {delta:>8} {noise:>9}"
                   + ("  beyond noise" if r["beyondNoise"] else ""))
    out.append(f"  classification: {CLASS_TEXT.get(record.get('classification'), record.get('classification'))}")
    for s in (record.get("worstScenarios") or [])[:5]:
        out.append(f"  scenario {s['scenarioId']} ({s['category']}): GoalSuccessRate {s['mean']:.2f} over {s['n']}")
    if record.get("experiment"):
        out.append(f"  measured through shadow experiment {record['experiment']} on dev (finished; dev back on /current)")
    return out


def worst_scenarios(results: Iterable[Mapping[str, Any]], limit: int = 5) -> list[dict]:
    by: dict[tuple[str, str], list[float]] = {}
    for r in results:
        if r.get("evaluatorId") == "Builtin.GoalSuccessRate" and r.get("value") is not None:
            by.setdefault((r["scenarioId"], r["category"]), []).append(float(r["value"]))
    rows = [{"scenarioId": s, "category": c, "mean": sum(v) / len(v), "n": len(v)} for (s, c), v in by.items()]
    return sorted([r for r in rows if r["mean"] < 1.0], key=lambda r: (r["mean"], r["scenarioId"]))[:limit]


def models_override(models_yaml: str, candidate_model: str, foundation_model: str) -> str:
    """models.yaml text with every agent's modelId and foundationModelId set to the candidate (comments kept)."""
    import re
    text = re.sub(r"(?m)^(\s+modelId:)\s*\S+\s*$", rf"\g<1> {candidate_model}", models_yaml)
    return re.sub(r"(?m)^(\s+foundationModelId:)\s*\S+\s*$", rf"\g<1> {foundation_model}", text)


def branch_for(profile: str) -> str:
    """`model/<profile without its geography prefix>` (a bot branch prefix vercel.json disables)."""
    import re
    slug = profile.split(".", 1)[-1] if profile.count(".") >= 2 else profile
    slug = re.sub(r"[^a-z0-9.-]+", "-", slug.lower()).strip("-")
    if not slug:
        raise ValueError("invalid inference profile")
    return f"model/{slug}"
