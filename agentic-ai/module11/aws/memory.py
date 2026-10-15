# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Memory incident notes with a release discriminator.

Namespaces are `/devops-companion/<stage>/release/<slug>/`. Measured: 
`namespacePath` is a path-segment-aware prefix and that `metadataFilters` work on the indexed
keys (service, variant, release), so reads list `/devops-companion/<stage>/` filtered by
service, across releases. New records take ~75-90 s to list, which only live tests see.

A `memoryId` of the form `file:///<dir>` keeps records in `<dir>/records.jsonl` for local
runs on 127.0.0.1; any other value is a real Memory ID.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from module11.release import pins as P

_SEGMENT = re.compile(r"^[a-z0-9-]{1,40}$")
METADATA_KEYS = ("service", "variant", "release", "manifestVersion", "sessionId")


def stage_prefix(stage: str) -> str:
    if not _SEGMENT.match(stage or ""):
        raise ValueError("stage must match ^[a-z0-9-]{1,40}$")
    return f"/devops-companion/{stage}/"


def namespace(stage: str, release: str) -> str:
    """`/devops-companion/<stage>/release/<slug>/`; the slug is like `harness-v8`."""
    if not _SEGMENT.match(release or ""):
        raise ValueError("release slug must match ^[a-z0-9-]{1,40}$")
    return f"{stage_prefix(stage)}release/{release}/"


def _local_dir(memory_id: str) -> Path | None:
    parts = urlsplit(memory_id)
    return Path(unquote(parts.path)) if parts.scheme == "file" else None


def _data_plane():
    from . import client  # read at call time so offline tests can replace the factory
    return client("bedrock-agentcore")


def write_note(memory_id: str, *, stage: str, release: str, text: str, metadata: dict[str, str]) -> dict:
    """One record in the release namespace; metadata carries the envelope's identity fields."""
    ns = namespace(stage, release)
    meta = {k: str(metadata[k]) for k in METADATA_KEYS if metadata.get(k) is not None}
    now = datetime.now(timezone.utc)
    local = _local_dir(memory_id)
    if local is not None:
        local.mkdir(parents=True, exist_ok=True)
        record = {"memoryRecordId": f"mem-{uuid.uuid4()}", "namespaces": [ns], "content": {"text": text},
                  "metadata": meta, "createdAt": now.isoformat()}
        with (local / "records.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, sort_keys=True) + "\n")
        return {"memoryRecordId": record["memoryRecordId"], "namespace": ns}
    response = _data_plane().batch_create_memory_records(memoryId=memory_id, records=[{
        "requestIdentifier": uuid.uuid4().hex, "namespaces": [ns], "content": {"text": text},
        "timestamp": now, "metadata": {k: {"stringValue": v} for k, v in meta.items()},
    }])
    failed = response.get("failedRecords") or []
    if failed:
        raise RuntimeError(f"memory write failed: {failed[0].get('errorCode', 'unknown')}")
    created = (response.get("successfulRecords") or [{}])[0]
    return {"memoryRecordId": created.get("memoryRecordId"), "namespace": ns}


# ---------------------------------------------------------------- purge
PURGE_PAGE = 100
THROTTLE_RETRIES = 3
TERMINAL = ("ROLLED_BACK", "PROMOTED")
# Reconciliation: Memory's list index shows a new record only ~45-90 s after it is written (measured
# live), and keeps showing a deleted one for a while. The release's side-effect objects in S3, one per
# record_incident_note call and written before its Memory record, are the strongly consistent count N; the purge
# deletes what Memory lists and re-lists every SETTLE_POLL_SECONDS until it has removed N records, or gives up
# after SETTLE_SECONDS.
SETTLE_SECONDS = 120
SETTLE_POLL_SECONDS = 5
PURGE_RECORD_SCHEMA = "m11.purge/1"


class PurgeNotReady(RuntimeError):
    """The purge gave up before deleting anything (exit 5)."""


@dataclass
class PurgeReport:
    namespace: str
    listed: int = 0
    deleted: int = 0
    failed: list[dict] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    already_gone: int = 0      # listed, but the delete answered 404: the index still showed a deleted record
    expected: int | None = None   # N, the release's side-effect objects in S3 (None: no reconciliation)
    removed: dict[str, str | None] = field(default_factory=dict)   # record ID -> sessionId, this run and earlier runs
    missing: list[str] = field(default_factory=list)   # side-effect keys whose Memory record was never found
    waited_seconds: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass
class PurgeReady:
    at: datetime
    case: str
    waited_seconds: float


def _throttled(exc: Exception) -> bool:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}


def _retry(call, *, sleep=time.sleep):
    for attempt in range(THROTTLE_RETRIES + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - throttles are retried 3 times with backoff
            if not _throttled(exc) or attempt == THROTTLE_RETRIES:
                raise
            sleep(2 ** attempt)
    return None  # pragma: no cover


def _session_of(metadata: Mapping | None) -> str | None:
    value = (metadata or {}).get("sessionId")
    return value.get("stringValue") if isinstance(value, Mapping) else value


def _summaries(memory_id: str, *, stage: str, release: str, data_plane: Any = None,
               sleep=time.sleep) -> list[tuple[str, str | None]]:
    """(record ID, sessionId) for every record Memory lists in the release namespace (pages of 100)."""
    ns = namespace(stage, release)
    local = _local_dir(memory_id)
    if local is not None:
        path = local / "records.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
            if path.is_file() else []
        return [(r["memoryRecordId"], _session_of(r.get("metadata"))) for r in rows if ns in r.get("namespaces", [])]
    plane = data_plane or _data_plane()
    out, token = [], None
    while True:
        kwargs = {"memoryId": memory_id, "namespacePath": ns, "maxResults": PURGE_PAGE}
        if token:
            kwargs["nextToken"] = token
        page = _retry(lambda: plane.list_memory_records(**kwargs), sleep=sleep)
        out += [(r["memoryRecordId"], _session_of(r.get("metadata"))) for r in page.get("memoryRecordSummaries") or []
                if ns in (r.get("namespaces") or [ns])]
        token = page.get("nextToken")
        if not token:
            return out


def list_release(memory_id: str, *, stage: str, release: str, data_plane: Any = None, sleep=time.sleep) -> list[str]:
    """Every record ID in the release namespace (pages of 100)."""
    return [i for i, _ in _summaries(memory_id, stage=stage, release=release, data_plane=data_plane, sleep=sleep)]


def note_session(key: str) -> str:
    """The sessionId of a side-effect key `side-effects/<stage>/<release>/<sessionId>/<sequence>.json`."""
    parts = key.split("/")
    if len(parts) != 5 or parts[0] != "side-effects":
        raise ValueError(f"not a side-effect key: {key}")
    return parts[3]


def _shortfall(notes: Sequence[str], removed: Mapping[str, str | None]) -> dict[str, int]:
    """Per sessionId: notes in S3 minus records removed for that session (never negative)."""
    want: dict[str, int] = {}
    for key in notes:
        want[note_session(key)] = want.get(note_session(key), 0) + 1
    have: dict[str, int] = {}
    for session in removed.values():
        if session is not None:
            have[session] = have.get(session, 0) + 1
    return {s: n - have.get(s, 0) for s, n in want.items() if n > have.get(s, 0)}


def _delete(memory_id: str, ids: list[str], plane: Any, sleep) -> tuple[list[str], list[str], list[dict]]:
    """(deleted, already gone (404), failed) for `ids`, in batches of 100."""
    local = _local_dir(memory_id)
    if local is not None:
        path = local / "records.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []
        present = {json.loads(line)["memoryRecordId"] for line in lines}
        path.write_text("".join(line + "\n" for line in lines if json.loads(line)["memoryRecordId"] not in set(ids)),
                        encoding="utf-8")
        return [i for i in ids if i in present], [i for i in ids if i not in present], []
    deleted, gone, failures = [], [], []
    for i in range(0, len(ids), PURGE_PAGE):
        batch = ids[i:i + PURGE_PAGE]
        out = _retry(lambda: plane.batch_delete_memory_records(
            memoryId=memory_id, records=[{"memoryRecordId": r} for r in batch]), sleep=sleep)
        failed = out.get("failedRecords") or []
        # The list index keeps showing a deleted record for a while; deleting it again answers 404
        # "Memory record not found". That record is gone, so it counts as removed, not as a failure.
        gone_now = [str(f.get("memoryRecordId")) for f in failed if str(f.get("errorCode")) == "404"]
        gone += gone_now
        failures += [{"memoryRecordId": f.get("memoryRecordId"), "errorCode": f.get("errorCode")}
                     for f in failed if str(f.get("memoryRecordId")) not in gone_now]
        bad = {str(f.get("memoryRecordId")) for f in failed}
        deleted += [r for r in batch if r not in bad]
    return deleted, gone, failures


def purge(memory_id: str, *, stage: str, release: str, dry_run: bool = True, data_plane: Any = None,
          sleep: Callable[[float], None] = time.sleep, notes: Sequence[str] | None = None,
          removed_before: Mapping[str, str | None] | None = None, clock: Callable[[], float] = time.monotonic,
          settle_seconds: float = SETTLE_SECONDS, poll_seconds: float = SETTLE_POLL_SECONDS,
          log: Callable[[str], None] = lambda line: None) -> PurgeReport:
    """List the release namespace, then (unless dry_run) BatchDeleteMemoryRecords in batches of 100. Only that
    release's namespace is touched; partial failures are reported, not raised.

    With `notes` (the release's side-effect keys in S3; N = len(notes)) the purge reconciles: it re-lists every
    `poll_seconds`, deleting what newly appears, until the records removed (deleted, or already gone: 404) cover
    every note per session, printing "waiting for k of N notes to reach Memory", or until `settle_seconds` pass;
    then `report.missing` names the notes whose record was never found (exit 5). `removed_before` holds the
    records earlier purges of this release removed (the purge record), so a re-run does not wait for them.
    A dry run lists once and reports N from S3."""
    report = PurgeReport(namespace(stage, release), expected=None if notes is None else len(notes),
                         removed=dict(removed_before or {}))
    plane = None if _local_dir(memory_id) is not None else (data_plane or _data_plane())
    started = clock()
    last_said = (None, -1e9)
    first = True
    while True:
        rows = _summaries(memory_id, stage=stage, release=release, data_plane=plane, sleep=sleep)
        if first:
            report.listed, report.ids, first = len(rows), [i for i, _ in rows], False
        todo = [(i, s) for i, s in rows if i not in report.removed]
        if dry_run:
            return report
        if todo:
            sessions = dict(todo)
            deleted, gone, failed = _delete(memory_id, [i for i, _ in todo], plane, sleep)
            for i in deleted + gone:
                report.removed[i] = sessions.get(i)
            report.deleted += len(deleted)
            report.already_gone += len(gone)
            report.failed += failed
        if notes is None:
            return report
        short = _shortfall(notes, report.removed)
        waiting = sum(short.values())
        report.waited_seconds = clock() - started
        if not waiting:
            return report
        if report.waited_seconds >= settle_seconds:
            report.missing = sorted(k for k in notes if note_session(k) in short)
            return report
        now = clock()
        if waiting != last_said[0] or now - last_said[1] >= 15:
            log(f"waiting for {waiting} of {len(notes)} notes to reach Memory ({report.waited_seconds:.0f} s)")
            last_said = (waiting, now)
        sleep(poll_seconds)


def purge_record_key(stage: str, release: str) -> str:
    """Where `memory purge` keeps the records it removed for a release (release bucket)."""
    namespace(stage, release)       # validates both segments
    return f"purges/{stage}/{release}.json"


def purge_record(stage: str, release: str, report: PurgeReport, at: datetime) -> dict:
    return {"schema": PURGE_RECORD_SCHEMA, "stage": stage, "release": release, "notes": report.expected,
            "removed": dict(sorted(report.removed.items())), "missing": report.missing, "updatedAt": at.isoformat()}


def _check_driver(driver: Any) -> dict | None:
    """The `traffic` record of demo-state.json, validated (h.4 table): an object, a string rolloutId,
    and ISO 8601 timestamps with an offset. Malformed -> PurgeNotReady (a running driver cannot be ruled out)."""
    if driver is None:
        return None
    if not isinstance(driver, dict):
        raise PurgeNotReady("demo-state.json unreadable: traffic is not an object; fix or remove it")
    if "rolloutId" in driver and not isinstance(driver["rolloutId"], str):
        raise PurgeNotReady("demo-state.json unreadable: traffic.rolloutId is not a string; fix or remove it")
    for key in ("startedAt", "finishedAt", "killedAt"):
        if driver.get(key) is not None:
            if not isinstance(driver[key], str):
                raise PurgeNotReady(f"demo-state.json unreadable: traffic.{key} is not an ISO 8601 string")
            try:
                P.ts(driver[key])
            except P.PinsViolation as exc:
                raise PurgeNotReady(f"demo-state.json unreadable: traffic.{key}: {exc}; fix or remove it") from exc
    return driver


def _case(rollout: Mapping, driver: Mapping | None) -> str:
    if driver and driver.get("rolloutId") and driver["rolloutId"] == rollout.get("rolloutId"):
        return "traffic.finishedAt" if driver.get("finishedAt") else "traffic.killedAt + deadlineSeconds + 30 s"
    terminal = "PROMOTED" if not rollout.get("rolledBackAt") and rollout.get("promotedAt") else "ROLLED_BACK"
    return f"{terminal} + deadlineSeconds + 30 s (no driver state for this rollout)"


def purge_wait(read_rollout: Callable[[], Mapping | None], read_driver: Callable[[], Any], candidate_pins: Mapping, *,
               clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
               poll_s: float = 5.0, no_time_limit_s: float = 120.0,
               log: Callable[[str], None] = lambda line: None) -> PurgeReady:
    """The purge-wait rule. Poll the control item and demo-state.json every `poll_s` until
    `purge_ready_at` gives a time, then stop reading, wait until it (steps <= 5 s) and return.

    PurgeNotReady (nothing deleted): `no_time_limit_s` from this call's start with no time (rollout not
    terminal, or this rollout's driver still running); a time more than deadlineSeconds + 40 s ahead;
    3 control-item read errors in a row; a malformed demo-state.json. PinsViolation propagates unchanged."""
    started = clock()
    deadline = P.granted_deadline_seconds(candidate_pins)
    errors = 0
    log(f"purge waits for: the rollout terminal and no candidate session able to write "
        f"(deadlineSeconds {deadline}; gives up after {int(no_time_limit_s)} s without a ready time)")
    while True:
        waiting = "the rollout to reach ROLLED_BACK or PROMOTED"
        try:
            rollout = read_rollout()
            errors = 0
        except Exception as exc:  # noqa: BLE001 - a read error is retried at the next poll
            errors += 1
            if errors >= 3:
                raise PurgeNotReady(f"3 control-item read errors in a row ({type(exc).__name__}); nothing deleted") \
                    from exc
            rollout = None
        if rollout is not None and rollout.get("state") in TERMINAL:
            try:
                raw = read_driver()
            except Exception as exc:  # noqa: BLE001 - unreadable file: a running driver cannot be ruled out
                raise PurgeNotReady(f"demo-state.json unreadable: {exc}; fix or remove it") from exc
            driver = _check_driver(raw)
            ready = P.purge_ready_at(rollout, candidate_pins, driver)
            if ready is not None:
                case = _case(rollout, driver)
                break
            waiting = f"rollout {rollout.get('rolloutId')}'s traffic driver to finish (traffic.finishedAt)"
        if clock() - started >= no_time_limit_s:
            raise PurgeNotReady(f"no ready time after {int(no_time_limit_s)} s: waiting for {waiting}; nothing deleted")
        sleep(poll_s)
    ahead = ready.timestamp() - clock()
    if ahead > deadline + 40:
        raise PurgeNotReady(f"ready time {ready.isoformat()} is {int(ahead)} s ahead, more than deadlineSeconds + 40 s "
                            f"({deadline + 40} s): a clock or data error; nothing deleted")
    log(f"purge starts at {ready.isoformat()} ({case})" + (f", in {int(ahead)} s" if ahead > 0 else ", now"))
    while (remaining := ready.timestamp() - clock()) > 0:
        sleep(min(5.0, remaining))
    return PurgeReady(ready, case, clock() - started)


def _summary(item: dict[str, Any]) -> dict:
    meta = item.get("metadata") or {}
    flat = {k: (v.get("stringValue") if isinstance(v, dict) else v) for k, v in meta.items()}
    created = item.get("createdAt")
    return {"text": (item.get("content") or {}).get("text", ""), "release": flat.get("release"),
            "namespace": (item.get("namespaces") or [None])[0],
            "createdAt": created.isoformat() if hasattr(created, "isoformat") else created}


def read_notes(memory_id: str, *, stage: str, service: str, limit: int = 5) -> list[dict]:
    """Prior notes for a service across every release of the stage, newest first."""
    prefix = stage_prefix(stage)
    local = _local_dir(memory_id)
    if local is not None:
        path = local / "records.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
            if path.is_file() else []
        hits = [r for r in rows if any(n.startswith(prefix) for n in r.get("namespaces", []))
                and (r.get("metadata") or {}).get("service") == service]
        hits.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
        return [_summary(r) for r in hits[:limit]]
    response = _data_plane().list_memory_records(
        memoryId=memory_id, namespacePath=prefix, maxResults=max(1, min(int(limit), 100)),
        metadataFilters=[{"left": {"metadataKey": "service"}, "operator": "EQUALS_TO",
                          "right": {"metadataValue": {"stringValue": service}}}])
    items = response.get("memoryRecordSummaries") or []
    items.sort(key=lambda r: str(r.get("createdAt", "")), reverse=True)
    return [_summary(r) for r in items[:limit]]
