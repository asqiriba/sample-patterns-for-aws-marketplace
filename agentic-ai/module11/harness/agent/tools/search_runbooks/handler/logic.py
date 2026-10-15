# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""search_runbooks: keyword retrieval over the pinned policy-corpus snapshot (Module 9 chunks,
design 4.2.6, 4.4.13). The snapshot is read from `context.data.snapshot` after the `m11-sha256`
check, and cached per hash in a warm Lambda environment (snapshots are immutable).
C3 checks that run()'s parameters, minus the trailing `data`, equal schema.json's properties.
"""
from __future__ import annotations

import json
import math
import re

from module11.aws import objects

TOP_K = 3   # retrieval.yaml topK for the policy corpus
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has have in is it of on or the this to was were with "
                  "what when why how after before since our we you your".split())
_CACHE: dict[str, dict] = {}


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def _snapshot(data: dict) -> dict:
    ref = data.get("snapshot") or {}
    if not ref.get("uri"):
        raise ValueError("context.data.snapshot.uri is missing")
    key = str(ref.get("sha256"))
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = json.loads(objects.verified_get(ref["uri"], ref.get("sha256")))
    return _CACHE[key]


def run(query: str, data: dict) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a nonempty string")
    snapshot = _snapshot(data)
    chunks = snapshot.get("chunks") or []
    docs = [set(_terms(c["title"] + " " + c["text"])) for c in chunks]
    wanted = set(_terms(query))
    scored = []
    for chunk, words in zip(chunks, docs):
        title = set(_terms(chunk["title"] + " " + chunk["docId"]))
        score = 0.0
        for term in wanted & words:
            df = sum(1 for d in docs if term in d)
            score += math.log(1 + len(docs) / df) * (2.0 if term in title else 1.0)
        if score > 0:
            scored.append((round(score, 6), chunk))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["chunkId"]))
    results = [{"docId": c["docId"], "title": c["title"], "chunkId": c["chunkId"], "score": s, "text": c["text"]}
               for s, c in scored[:TOP_K]]
    summary = (f"Top match: {results[0]['docId']} ({results[0]['title']})." if results
               else "No runbook matched the query.")
    return {"source": "m11-policy-snapshot", "snapshotId": snapshot.get("snapshotId"), "summary": summary,
            "results": results}
