# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Immutable policy-corpus snapshots: Module 9's snapshot step, offline.

Sources are the policy documents in `module9.mock.seed_corpora_data.SEED_DOCS` plus
`module11/corpus/runbooks/*.md`. Each document is chunked with
`module9.ingestion.chunk.chunk_record(content, "policy")` and every chunk passes
`module9.ingestion.quality.run_quality_gates(text, dedup_index=idx)` with a fresh `DedupIndex()`
per build (Module 9's module-level index is process-global, so reusing it would drop every
chunk on a second build as a duplicate).

The snapshot is canonical JSON, so the same sources give the same bytes, `snapshotId`
(`pol-<12 hex>` of the content) and `snapshotSha256` on every machine. Seal uploads it to
`snapshots/policy/<snapshotId>.json` with metadata `m11-sha256`; `retrieval.yaml` pins all three
values, and `search_runbooks` refuses a snapshot whose metadata does not match the pin.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from module9.ingestion.chunk import chunk_record
from module9.ingestion.quality import DedupIndex, run_quality_gates
from module9.mock.seed_corpora_data import SEED_DOCS

SCHEMA = "m11.snapshot/1"
CORPUS = "policy"
RUNBOOKS = "module11/corpus/runbooks"
RETRIEVAL = "module11/harness/agent/retrieval.yaml"
BUILT_FROM = "module9.chunk+quality"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _seed_id(source_uri: str) -> str:
    return "seed-" + re.sub(r"[^a-z0-9-]+", "-", source_uri.rsplit("/", 1)[-1].lower()).strip("-")


def sources(repo_root: str | Path) -> list[dict]:
    """[{docId, title, source, content}] in a fixed order: Module 9 seeds, then runbooks by name.
    `source_modified` is left out: Module 9 dates its seeds relative to today."""
    root = Path(repo_root)
    docs = [{"docId": _seed_id(d["source_uri"]), "title": d["source_uri"].rsplit("/", 1)[-1].replace("-", " ").capitalize(),
             "source": d["source_uri"], "content": d["content"].strip()}
            for d in SEED_DOCS if d.get("corpus") == CORPUS]
    for path in sorted((root / RUNBOOKS).glob("*.md")):
        lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), path.stem)
        body = " ".join(line.strip() for line in lines if line.strip() and not line.startswith("#"))
        docs.append({"docId": path.stem, "title": title, "source": f"{RUNBOOKS}/{path.name}", "content": body})
    return docs


def build(repo_root: str | Path, docs: list[dict] | None = None) -> tuple[bytes, dict]:
    """(snapshot bytes, pin {snapshotId, snapshotSha256, documentCount, chunkCount}); `docs` defaults to
    `sources(repo_root)` (`corpus publish --change` passes the changed set)."""
    index = DedupIndex()
    documents, chunks, rejected = [], [], []
    for doc in (sources(repo_root) if docs is None else docs):
        documents.append({"docId": doc["docId"], "title": doc["title"], "source": doc["source"]})
        for chunk in chunk_record(doc["content"], CORPUS):
            report = run_quality_gates(chunk.content, dedup_index=index)
            if not report.passed:
                rejected.append({"docId": doc["docId"], "chunkIndex": chunk.chunk_index, "verdict": report.verdict,
                                 "checks": [c["check"] for c in report.checks if not c["passed"]]})
                continue
            index.record(report.content_hash, doc["docId"])
            chunks.append({"chunkId": f"{doc['docId']}#{chunk.chunk_index}", "docId": doc["docId"],
                           "title": doc["title"], "source": doc["source"], "text": chunk.content,
                           "chunkIndex": chunk.chunk_index, "chunkCount": chunk.chunk_count,
                           "strategy": chunk.chunking_strategy, "contentHash": report.content_hash})
    body = {"schema": SCHEMA, "corpus": CORPUS, "builtFrom": BUILT_FROM,
            "documents": documents, "chunks": chunks, "rejected": rejected}
    snapshot_id = "pol-" + hashlib.sha256(_canonical(body)).hexdigest()[:12]
    data = _canonical({**body, "snapshotId": snapshot_id})
    pin = {"snapshotId": snapshot_id, "snapshotSha256": "sha256:" + hashlib.sha256(data).hexdigest(),
           "documentCount": len(documents), "chunkCount": len(chunks)}
    return data, pin


def object_key(snapshot_id: str) -> str:
    """Release-bucket key Seal uploads to (immutable, content-addressed)."""
    return f"snapshots/{CORPUS}/{snapshot_id}.json"


def write(repo_root: str | Path, out_dir: str | Path) -> tuple[Path, dict]:
    """Write `<out_dir>/snapshots/policy/<id>.json` plus its metadata sidecar (module11.aws.objects)."""
    data, pin = build(repo_root)
    path = Path(out_dir) / object_key(pin["snapshotId"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    Path(str(path) + ".metadata.json").write_text(json.dumps({"m11-sha256": pin["snapshotSha256"]}), encoding="utf-8")
    return path, pin


def pinned(repo_root: str | Path) -> dict:
    """The policy entry of retrieval.yaml."""
    import yaml
    doc = yaml.safe_load((Path(repo_root) / RETRIEVAL).read_text(encoding="utf-8")) or {}
    return next((c for c in doc.get("corpora", []) if c.get("corpus") == CORPUS), {})


def update_pin(repo_root: str | Path) -> bool:
    """Rewrite the three pinned values in retrieval.yaml, keeping comments; True when changed."""
    from .pin import pin_text
    _, pin = build(repo_root)
    path = Path(repo_root) / RETRIEVAL
    text = path.read_text(encoding="utf-8")
    new = pin_text(text, CORPUS, pin)
    if new != text:
        path.write_text(new, encoding="utf-8")
    return new != text
