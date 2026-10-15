# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Session spans for `Evaluate`.

The pure half lives here and needs no AWS: `filter_pattern()` quotes the session ID for
FilterLogEvents (filter patterns match terms with non-alphanumeric characters only inside double
quotes, and session IDs contain hyphens), and `select_records()` keeps only JSON records whose
`scope.name` is set and whose `attributes["session.id"]` (the literal dotted key, never a nested
path) equals the session, with the `aws/spans` documents first and the runtime log group's
records after them.

The live half:
- `session_spans()` pages FilterLogEvents with the quoted term filter over `aws/spans` and the runtime
  log group, in the session's `[start - 1 min, end + 10 min]` window, and hands the raw events to
  `select_records()`. FilterLogEvents may return empty pages that carry a nextToken, so it pages to the end.
- `readiness_counts()` is the Logs Insights query of the runner's wait: one query per poll over every
  pending session, one row per session (record count and whether an invoke_agent record exists).
- `Tracker` and `wait_for_spans()` turn those counts into the design's readiness rule: a session is ready
  when its invoke_agent record exists and its record count has not changed for 20 s; polling starts 60 s
  after the last invocation, polls every 15 s and gives up 6 minutes after it (docs: logs take "a couple of
  minutes"). Sessions still missing at the deadline are `spans_missing`.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

MAX_SPANS = 20_000          # Evaluate accepts 1-20,000 spans
SPANS_SOURCE = "aws/spans"
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
WINDOW_BEFORE_SECONDS = 60
WINDOW_AFTER_SECONDS = 600
FIRST_POLL_DELAY = 60.0
POLL_INTERVAL = 15.0
STABLE_FOR = 20.0
GIVE_UP_AFTER = 360.0


class SpansError(RuntimeError):
    """`spans_too_large` or `spans_missing`; both feed the gate's coverage rule."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def filter_pattern(session_id: str) -> str:
    """A quoted term filter: `"<session id>"`."""
    if not _SESSION_ID.match(session_id):
        raise ValueError("session ID has characters a quoted filter term cannot carry")
    return f'"{session_id}"'


def _record(message: Any) -> dict | None:
    if isinstance(message, Mapping):
        return dict(message)
    try:
        value = json.loads(message)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def select_records(raw: Iterable[Mapping[str, Any]], session_id: str) -> list[dict]:
    """`raw`: FilterLogEvents events, each {"source": "aws/spans" | <runtime log group>, "message"}.
    Returns the matching records, aws/spans first, each source in its input order."""
    spans, runtime = [], []
    for event in raw:
        record = _record(event.get("message"))
        if record is None:
            continue
        scope = record.get("scope") or {}
        attrs = record.get("attributes") or {}
        if not (isinstance(scope, Mapping) and scope.get("name")):
            continue
        if not isinstance(attrs, Mapping) or attrs.get("session.id") != session_id:
            continue
        (spans if event.get("source") == SPANS_SOURCE else runtime).append(record)
    records = spans + runtime
    if len(records) > MAX_SPANS:
        raise SpansError("spans_too_large", f"{len(records)} records for {session_id} (limit {MAX_SPANS})")
    if not records:
        raise SpansError("spans_missing", f"no records for {session_id}")
    return records


# ---------------------------------------------------------------- live: fetch
def _code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _filter_events(logs: Any, group: str, session_id: str, start: float, end: float) -> Iterable[Mapping[str, Any]]:
    token = None
    while True:
        kwargs: dict[str, Any] = {"logGroupName": group, "filterPattern": filter_pattern(session_id),
                                  "startTime": int((start - WINDOW_BEFORE_SECONDS) * 1000),
                                  "endTime": int((end + WINDOW_AFTER_SECONDS) * 1000)}
        if token:
            kwargs["nextToken"] = token
        try:
            page = logs.filter_log_events(**kwargs)
        except Exception as exc:
            if _code(exc) == "ResourceNotFoundException":      # aws/spans is empty under unified delivery
                return
            raise
        yield from page.get("events", [])
        token = page.get("nextToken")
        if not token:
            return


def session_spans(session_id: str, runtime_log_group: str, start: float, end: float, *, logs: Any = None
                  ) -> list[dict]:
    """The session's span documents for `evaluationInput.sessionSpans`.

    `start`/`end` are epoch seconds of the session's first and last invocation. `logs` is a CloudWatch Logs
    client (the eval-runner's own session in the runner); it defaults to the account factory.
    Raises SpansError(`spans_missing` | `spans_too_large`)."""
    if logs is None:
        from . import client
        logs = client("logs")
    raw = [{"source": group, "message": event["message"]}
           for group in (SPANS_SOURCE, runtime_log_group)
           for event in _filter_events(logs, group, session_id, start, end)]
    return select_records(raw, session_id)


# ---------------------------------------------------------------- live: readiness
def insights_query(prefix: str) -> str:
    """One row per session whose records carry `prefix`: record count and invoke_agent record count."""
    if not re.match(r"^[A-Za-z0-9_.:-]{1,200}$", prefix):
        raise ValueError("prefix has characters a query term cannot carry")
    return (f'fields @message | filter @message like "{prefix}" '
            '| parse @message /"session\\.id":\\s*"(?<sid>[^"]+)"/ | filter ispresent(sid) '
            '| stats count(*) as n, sum(strcontains(@message, "invoke_agent")) as agents by sid | limit 10000')


def parse_counts(rows: Iterable[Iterable[Mapping[str, str]]]) -> dict[str, tuple[int, int]]:
    """GetQueryResults rows -> {session ID: (records, invoke_agent records)}."""
    out: dict[str, tuple[int, int]] = {}
    for row in rows:
        fields = {cell["field"]: cell["value"] for cell in row if "field" in cell}
        sid = fields.get("sid")
        if sid:
            out[sid] = (int(float(fields.get("n") or 0)), int(float(fields.get("agents") or 0)))
    return out


def readiness_counts(logs: Any, group: str, prefix: str, start: float, end: float, *,
                     sleep: Callable[[float], None] = time.sleep, timeout: float = 90.0) -> dict[str, tuple[int, int]]:
    """Run the Logs Insights readiness query once and return its counts."""
    query = logs.start_query(logGroupName=group, startTime=int(start), endTime=int(end) + 1,
                             queryString=insights_query(prefix), limit=10000)["queryId"]
    waited = 0.0
    while True:
        result = logs.get_query_results(queryId=query)
        status = result.get("status")
        if status == "Complete":
            return parse_counts(result.get("results", []))
        if status in {"Failed", "Cancelled", "Timeout"}:
            raise RuntimeError(f"readiness query {status}")
        if waited >= timeout:
            raise RuntimeError("readiness query did not finish in time")
        sleep(1.0)
        waited += 1.0


@dataclass
class Tracker:
    """The pure readiness rule. `observe()` takes the counts of one poll and the poll's clock time."""
    session_ids: Iterable[str]
    stable_for: float = STABLE_FOR
    _seen: dict[str, tuple[int, float]] = field(default_factory=dict)      # sid -> (count, time it last changed)
    _agents: dict[str, int] = field(default_factory=dict)
    _ready: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.session_ids = list(self.session_ids)

    def observe(self, counts: Mapping[str, tuple[int, int]], now: float) -> list[str]:
        """Returns the sessions that became ready at this poll."""
        newly = []
        for sid in self.session_ids:
            if sid in self._ready or sid not in counts:
                continue
            n, agents = counts[sid]
            previous = self._seen.get(sid)
            if previous is None or previous[0] != n:
                self._seen[sid] = (n, now)
            self._agents[sid] = agents
            if agents > 0 and now - self._seen[sid][1] >= self.stable_for:
                self._ready.add(sid)
                newly.append(sid)
        return newly

    @property
    def ready(self) -> set[str]:
        return set(self._ready)

    @property
    def pending(self) -> list[str]:
        return [sid for sid in self.session_ids if sid not in self._ready]


def wait_for_spans(poll: Callable[[], Mapping[str, tuple[int, int]]], session_ids: Iterable[str], *,
                   last_invoke_at: float, on_ready: Callable[[str], None] = lambda sid: None,
                   clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                   first_poll_delay: float = FIRST_POLL_DELAY, interval: float = POLL_INTERVAL,
                   give_up_after: float = GIVE_UP_AFTER, stable_for: float = STABLE_FOR,
                   log: Callable[[str], None] = lambda line: None) -> tuple[set[str], list[str]]:
    """Poll until every session is ready or `give_up_after` seconds after the last invocation.
    Returns (ready, missing); `on_ready(sid)` fires as each session becomes ready."""
    tracker = Tracker(session_ids, stable_for=stable_for)
    first = last_invoke_at + first_poll_delay
    deadline = last_invoke_at + give_up_after
    if clock() < first:
        sleep(first - clock())
    while True:
        now = clock()
        try:
            for sid in tracker.observe(poll(), now):
                on_ready(sid)
        except Exception as exc:                       # a failed poll is one lost look, not a lost run
            log(f"readiness poll failed: {type(exc).__name__}: {str(exc)[:120]}")
        if not tracker.pending:
            break
        if clock() >= deadline:
            break
        sleep(max(0.0, min(interval, deadline - clock())))
    return tracker.ready, tracker.pending
