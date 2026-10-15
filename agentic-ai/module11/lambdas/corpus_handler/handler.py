# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The corpus-event handler `m11-corpus-handler`: a `CorpusSnapshotPublished` event on
the `m11-releases` bus becomes a GitLab change request.

Steps:
1. Validate the event (`corpus/pin.py: validate_event`).
2. `HeadObject` the snapshot and compare its `m11-sha256` metadata with the event's hash.
3. Open (or reuse) the bot MR `corpus: <corpus> snapshot <id>` from branch `corpus/<corpus>/<snapshotId>`. It edits
   `retrieval.yaml`'s pin and carries the changed runbook sources (`snapshots/<corpus>/<id>.sources.json`), so the
   MR's tree builds the same snapshot bytes at Seal. Labels `change::retrieval-corpus`, plus `m11-demo-run` when the
   event carries `demoRun`. Duplicate events reuse the open MR (one idempotence rule).

Failure owners (M15):
- Fatal (validation failure, hash mismatch): no MR; the handler sends `{reason, event}` to `m11-corpus-dlq` itself
  and returns normally, so nothing retries it.
- Recoverable (GitLab 5xx or 429 after the client's own retries, a missing snapshot object, any other error): the
  handler raises; Lambda's async configuration retries twice within an hour, then its on-failure destination
  `m11-corpus-dlq` gets the record with the event and the error.
- Delivery failures (EventBridge cannot invoke the function): the rule target's retry policy and dead-letter queue.
The bot token (`devops-companion/partners/gitlab-token`) is read into memory only and never logged.
Packaged flat with `gitlab_api.py`, `bot_changes.py` and `pin.py` (standard library only).
"""
from __future__ import annotations

import json
import os
from typing import Any

try:  # flat in the Lambda zip; module11.* in the repo and tests
    from module11.aws import bot_changes as B
    from module11.aws import gitlab_api as G
    from module11.corpus import pin as P
except ImportError:  # pragma: no cover - the Lambda layout
    import bot_changes as B  # type: ignore[no-redef]
    import gitlab_api as G  # type: ignore[no-redef]
    import pin as P  # type: ignore[no-redef]


def log(level: str, event: str, **fields: Any) -> None:
    print(json.dumps({"level": level, "event": event, **fields}, default=str), flush=True)


class Fatal(Exception):
    """Not retried: the event goes to the DLQ with the reason."""


def _clients():  # pragma: no cover - AWS
    import boto3
    return boto3.client("s3"), boto3.client("sqs"), boto3.client("secretsmanager")


def check_snapshot(s3: Any, detail: dict, bucket: str) -> None:
    key = P.snapshot_key(detail["corpus"], detail["snapshotId"])
    head = s3.head_object(Bucket=bucket, Key=key)            # a missing object raises: recoverable (retried)
    got = (head.get("Metadata") or {}).get("m11-sha256")
    if got != detail["snapshotSha256"]:
        raise Fatal(f"snapshot hash mismatch: object {got}, event {detail['snapshotSha256']}")


def changed_sources(s3: Any, detail: dict, bucket: str) -> dict[str, bytes]:
    key = P.sources_key(detail["corpus"], detail["snapshotId"])
    try:
        doc = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read() or b"{}")
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return {}
        raise
    if not isinstance(doc, dict):
        raise Fatal("the sources object is not a map")
    out = {}
    for path, text in doc.items():
        if not P.SOURCE_PATH.match(str(path)) or path not in detail["sources"] or not isinstance(text, str):
            raise Fatal(f"source {path!r} is not one of the event's runbook sources")
        out[path] = text.encode("utf-8")
    return out


def open_request(gl: Any, detail: dict, sources: dict[str, bytes]) -> B.ChangeRequest:
    corpus, sid = detail["corpus"], detail["snapshotId"]
    branch = P.branch_for(corpus, sid)
    existing = gl.merge_requests(source_branch=branch, state="opened")
    if existing:                                             # duplicate event: reuse, change nothing
        mr = existing[0]
        return B.ChangeRequest(int(mr["iid"]), mr.get("web_url", ""), branch, True, mr.get("sha"))
    text = (gl.file(P.RETRIEVAL, "main") or b"").decode("utf-8")
    if not text:
        raise RuntimeError(f"{P.RETRIEVAL} is missing on main")
    previous = _previous_pin(text, corpus)
    new = P.pin_text(text, corpus, {"snapshotId": sid, "snapshotSha256": detail["snapshotSha256"],
                                    "documentCount": detail["documentCount"]})
    labels = [P.LABEL] + ([P.DEMO_LABEL] if detail.get("demoRun") else [])
    return B.open_change(gl, branch=branch, files={P.RETRIEVAL: new.encode("utf-8"), **sources},
                         message=f"{P.title_for(corpus, sid)}\n\nIngestion job {detail['ingestionJobId']}.",
                         title=P.title_for(corpus, sid), description=P.description_for(detail, previous),
                         labels=labels)


def _previous_pin(text: str, corpus: str) -> dict | None:
    import re
    block = re.search(rf"-\s*corpus:\s*{re.escape(corpus)}\s*\n((?:\s+\S.*\n?)*)", text)
    if not block:
        return None
    hit = re.search(r"snapshotId:\s*\"?([^\"\s]+)", block.group(1))
    return {"snapshotId": hit.group(1)} if hit else None


def to_dlq(sqs: Any, reason: str, event: Any) -> None:
    sqs.send_message(QueueUrl=os.environ["DLQ_URL"], MessageBody=json.dumps(
        {"source": "m11-corpus-handler", "reason": reason, "event": event}, default=str)[:250_000])


def handle(event: Any, *, s3: Any, sqs: Any, gitlab: Any, bucket: str) -> dict:
    try:
        detail = P.validate_event(event, bucket)
    except P.InvalidCorpusEvent as exc:
        to_dlq(sqs, f"invalid event: {exc}", event)
        log("ERROR", "corpus_event_invalid", reason=str(exc))
        return {"ok": False, "fatal": True, "reason": str(exc)}
    try:
        check_snapshot(s3, detail, bucket)
        sources = changed_sources(s3, detail, bucket)
    except Fatal as exc:
        to_dlq(sqs, str(exc), event)
        log("ERROR", "corpus_event_rejected", snapshotId=detail["snapshotId"], reason=str(exc))
        return {"ok": False, "fatal": True, "reason": str(exc)}
    try:
        change = open_request(gitlab(), detail, sources)
    except G.GitLabError as exc:                             # 5xx/429 after the client's retries: Lambda retries
        log("WARN", "corpus_gitlab_error", snapshotId=detail["snapshotId"], status=exc.status, error=str(exc))
        raise
    log("INFO", "corpus_change_request", snapshotId=detail["snapshotId"], iid=change.iid, reused=change.reused,
        branch=change.branch, demoRun=detail.get("demoRun"))
    return {"ok": True, "iid": change.iid, "webUrl": change.web_url, "reused": change.reused, "branch": change.branch}


def handler(event: Any, context: Any = None) -> dict:  # pragma: no cover - AWS wiring
    s3, sqs, secrets = _clients()

    def gitlab():
        return G.from_secret(secrets, os.environ["GITLAB_TOKEN_SECRET"], os.environ["M11_GITLAB_PROJECT_ID"])
    return handle(event, s3=s3, sqs=sqs, gitlab=gitlab, bucket=os.environ["RELEASE_BUCKET"])
