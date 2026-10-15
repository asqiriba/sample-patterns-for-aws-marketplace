# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`module11 corpus publish`: Module 9's snapshot step as a release event (B6 6d).

Module 9 built the ingestion pipeline; its partners run in mock mode here. This command performs its
snapshot step and announces the result:
1. Sources: the pinned corpus (`corpus/snapshot.py: sources`), with `--change <name>` applying
   `corpus/updates/<name>.md` (its first line `<!-- m11: replaces <runbook>.md -->` names the runbook it replaces;
   without it the update is a new runbook).
2. Build with Module 9's `chunk_record` and `run_quality_gates` and a fresh `DedupIndex` (`snapshot.build`).
3. Write the immutable snapshot `snapshots/<corpus>/<snapshotId>.json` (metadata `m11-sha256`, `If-None-Match`)
   and the changed source texts `snapshots/<corpus>/<snapshotId>.sources.json`, so the change request builds the
   same bytes at Seal.
4. Emit `CorpusSnapshotPublished` on the `m11-releases` bus. `M11_DEMO_RUN` is copied into the detail as `demoRun`.
The corpus handler (`lambdas/corpus_handler`) then opens the GitLab MR `corpus: <corpus> snapshot <id>`. The
deployed manifest keeps the old pin until that change is released.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import pin as P
from . import snapshot as S

UPDATES = "module11/corpus/updates"
REPLACES = re.compile(r"^<!--\s*m11:\s*replaces\s+([a-z0-9-]+\.md)\s*-->\s*$")
EVENT_BUS = "m11-releases"


class PublishError(RuntimeError):
    pass


def apply_change(root: Path, change: str | None) -> tuple[list[dict], dict[str, str], dict]:
    """(documents, changed source texts {repo path: text}, counts) for the snapshot with `change` applied."""
    docs = S.sources(root)
    counts = {"added": 0, "updated": 0, "deleted": 0}
    if not change:
        return docs, {}, counts
    if not re.fullmatch(r"[a-z0-9-]{1,80}", change):
        raise PublishError("--change must name a file in module11/corpus/updates/ (lowercase, digits, hyphens)")
    path = Path(root) / UPDATES / f"{change}.md"
    if not path.is_file():
        raise PublishError(f"{UPDATES}/{change}.md does not exist")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    hit = REPLACES.match(first.strip())
    target = hit.group(1) if hit else f"{change}.md"
    body = rest if hit else text
    repo_path = f"{S.RUNBOOKS}/{target}"
    lines = body.split("\n")
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), Path(target).stem)
    content = " ".join(line.strip() for line in lines if line.strip() and not line.startswith("#"))
    doc = {"docId": Path(target).stem, "title": title, "source": repo_path, "content": content}
    for i, existing in enumerate(docs):
        if existing["source"] == repo_path:
            docs[i] = doc
            counts["updated"] = 1
            break
    else:
        docs.append(doc)
        docs.sort(key=lambda d: (not d["source"].startswith(S.RUNBOOKS), d["source"]))
        counts["added"] = 1
    return docs, {repo_path: body.lstrip("\n")}, counts


def event_detail(*, corpus: str, pin: dict, bucket: str, counts: dict, sources: list[str], job_id: str,
                 demo_run: str | None) -> dict:
    detail = {"corpus": corpus, "snapshotId": pin["snapshotId"],
              "snapshotUri": f"s3://{bucket}/{P.snapshot_key(corpus, pin['snapshotId'])}",
              "snapshotSha256": pin["snapshotSha256"], "ingestionJobId": job_id, "documentCounts": counts,
              "documentCount": pin["documentCount"], "sources": sorted(sources)}
    if demo_run:
        detail["demoRun"] = demo_run
    return detail


def publish(root: Path, *, corpus: str, change: str | None, bucket: str, s3: Any, events: Any,
            log: Callable[[str], None] = print, demo_run: str | None = None) -> dict:
    """Build, store and announce one snapshot. Returns {pin, detail, eventId, written}."""
    from module11.aws import releases
    if corpus not in P.CORPORA:
        raise PublishError(f"unknown corpus {corpus!r}")
    docs, changed, counts = apply_change(Path(root), change)
    data, pin = S.build(root, docs=docs)
    current = S.pinned(root)
    if current.get("snapshotSha256") == pin["snapshotSha256"]:
        log(f"{corpus}: the sources build the pinned snapshot {pin['snapshotId']}; publishing it again")
    key = P.snapshot_key(corpus, pin["snapshotId"])
    written = releases.put_absent(s3, bucket, key, data, metadata={"m11-sha256": pin["snapshotSha256"]},
                                  content_type="application/json")
    releases.put_absent(s3, bucket, P.sources_key(corpus, pin["snapshotId"]),
                        json.dumps(changed, sort_keys=True).encode("utf-8"), content_type="application/json")
    log(f"snapshot {pin['snapshotId']} ({pin['documentCount']} documents, {pin['chunkCount']} chunks) "
        f"{'written to' if written else 'already at'} s3://{bucket}/{key}")
    job_id = f"m11-ingest-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    detail = event_detail(corpus=corpus, pin=pin, bucket=bucket, counts=counts, sources=list(changed), job_id=job_id,
                          demo_run=demo_run if demo_run is not None else os.environ.get("M11_DEMO_RUN"))
    P.validate_event({"source": P.SOURCE, "detail-type": P.DETAIL_TYPE, "detail": detail}, bucket)
    result = events.put_events(Entries=[{"Source": P.SOURCE, "DetailType": P.DETAIL_TYPE,
                                         "Detail": json.dumps(detail, sort_keys=True), "EventBusName": EVENT_BUS}])
    entry = (result.get("Entries") or [{}])[0]
    if result.get("FailedEntryCount") or not entry.get("EventId"):
        raise PublishError(f"PutEvents failed: {entry.get('ErrorCode')} {entry.get('ErrorMessage')}")
    log(f"event {P.DETAIL_TYPE} {entry['EventId']} on {EVENT_BUS}: the corpus handler opens "
        f"MR `{P.title_for(corpus, pin['snapshotId'])}`")
    return {"pin": pin, "detail": detail, "eventId": entry["EventId"], "written": written}
