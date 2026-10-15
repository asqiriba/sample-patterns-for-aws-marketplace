# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The corpus event's contract and the `retrieval.yaml` pin edit. Pure, standard library
only: the corpus handler Lambda packages this file as is, next to `gitlab_api.py` and `bot_changes.py`.

`validate_event(event, bucket)` returns the event's detail or raises `InvalidCorpusEvent` (fatal: the handler sends
it to the DLQ and returns). `pin_text(text, pin)` rewrites the three pinned values of one corpus in
`retrieval.yaml`, keeping comments. `branch_for(corpus, snapshot_id)` is the deterministic bot branch.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

SOURCE = "devops-companion.module9"
DETAIL_TYPE = "CorpusSnapshotPublished"
CORPORA = ("policy",)
LABEL = "change::retrieval-corpus"
DEMO_LABEL = "m11-demo-run"
RETRIEVAL = "module11/harness/agent/retrieval.yaml"
SNAPSHOT_ID = re.compile(r"^pol-[0-9a-f]{12}$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
DEMO_RUN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SOURCE_PATH = re.compile(r"^module11/corpus/runbooks/[a-z0-9-]{1,80}\.md$")
JOB_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_SOURCES = 50


class InvalidCorpusEvent(ValueError):
    pass


def snapshot_key(corpus: str, snapshot_id: str) -> str:
    return f"snapshots/{corpus}/{snapshot_id}.json"


def sources_key(corpus: str, snapshot_id: str) -> str:
    """The changed source files of a published snapshot (`{path: text}`), so its MR builds the same bytes."""
    return f"snapshots/{corpus}/{snapshot_id}.sources.json"


def branch_for(corpus: str, snapshot_id: str) -> str:
    return f"corpus/{corpus}/{snapshot_id}"


def title_for(corpus: str, snapshot_id: str) -> str:
    return f"corpus: {corpus} snapshot {snapshot_id}"


def validate_event(event: Mapping[str, Any], bucket: str) -> dict:
    """The detail of a `CorpusSnapshotPublished` event, checked field by field."""
    if not isinstance(event, Mapping):
        raise InvalidCorpusEvent("event is not an object")
    if event.get("source") != SOURCE or event.get("detail-type") != DETAIL_TYPE:
        raise InvalidCorpusEvent("unexpected source or detail-type")
    d = event.get("detail")
    if not isinstance(d, Mapping):
        raise InvalidCorpusEvent("detail is not an object")
    corpus, sid = d.get("corpus"), d.get("snapshotId")
    if corpus not in CORPORA:
        raise InvalidCorpusEvent("unknown corpus")
    if not isinstance(sid, str) or not SNAPSHOT_ID.match(sid):
        raise InvalidCorpusEvent("snapshotId is invalid")
    if d.get("snapshotUri") != f"s3://{bucket}/{snapshot_key(corpus, sid)}":
        raise InvalidCorpusEvent("snapshotUri is not the release bucket's snapshot key")
    if not isinstance(d.get("snapshotSha256"), str) or not SHA256.match(d["snapshotSha256"]):
        raise InvalidCorpusEvent("snapshotSha256 is invalid")
    job = d.get("ingestionJobId")
    if not isinstance(job, str) or not JOB_ID.match(job):
        raise InvalidCorpusEvent("ingestionJobId is invalid")
    counts = d.get("documentCounts")
    if not isinstance(counts, Mapping) or set(counts) != {"added", "updated", "deleted"} or \
            not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10_000 for v in counts.values()):
        raise InvalidCorpusEvent("documentCounts must be {added, updated, deleted} counts")
    sources = d.get("sources")
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES or \
            not all(isinstance(s, str) and SOURCE_PATH.match(s) for s in sources):
        raise InvalidCorpusEvent("sources must be runbook paths")
    documents = d.get("documentCount")
    if not isinstance(documents, int) or isinstance(documents, bool) or not 1 <= documents <= 10_000:
        raise InvalidCorpusEvent("documentCount is invalid")
    demo = d.get("demoRun")
    if demo is not None and (not isinstance(demo, str) or not DEMO_RUN.match(demo)):
        raise InvalidCorpusEvent("demoRun is invalid")
    return {"corpus": corpus, "snapshotId": sid, "snapshotUri": d["snapshotUri"], "snapshotSha256": d["snapshotSha256"],
            "ingestionJobId": job, "documentCounts": dict(counts), "documentCount": documents, "sources": list(sources),
            "demoRun": demo}


def pin_text(text: str, corpus: str, pin: Mapping[str, Any]) -> str:
    """`retrieval.yaml` with `corpus`'s snapshotId, snapshotSha256 and documentCount set (comments kept)."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if re.match(rf"^\s*-\s*corpus:\s*{re.escape(corpus)}\s*$", line)), None)
    if start is None:
        raise ValueError(f"{RETRIEVAL}: no `- corpus: {corpus}` entry")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*-\s", lines[i])), len(lines))
    for key in ("snapshotId", "snapshotSha256", "documentCount"):
        value = pin[key]
        rendered = str(value) if isinstance(value, int) else json.dumps(value)
        hit = next((i for i in range(start, end) if re.match(rf"^\s+{key}:", lines[i])), None)
        if hit is None:
            raise ValueError(f"{RETRIEVAL}: no `{key}:` line for {corpus}")
        lines[hit] = re.sub(rf"^(\s+{key}:).*$", rf"\g<1> {rendered}", lines[hit])
    return "\n".join(lines)


def description_for(detail: Mapping[str, Any], previous: Mapping[str, Any] | None) -> str:
    counts = detail["documentCounts"]
    was = f"`{previous.get('snapshotId')}`" if previous else "none"
    return "\n".join([
        f"Module 9 published policy snapshot `{detail['snapshotId']}` (ingestion job `{detail['ingestionJobId']}`).",
        "",
        f"- pin: {was} -> `{detail['snapshotId']}` (`{detail['snapshotSha256']}`)",
        f"- documents: {detail['documentCount']} ({counts['added']} added, {counts['updated']} updated, "
        f"{counts['deleted']} deleted)",
        f"- sources: {', '.join(f'`{s}`' for s in detail['sources']) or 'none'}",
        "",
        "The deployed manifest keeps the old pin until this change is released: change type `retrieval_corpus` "
        "(retrieval subset at the staging gate, canary on prod). Opened by the m11 corpus handler; "
        "the UI preview is skipped for bot branches.",
    ])
