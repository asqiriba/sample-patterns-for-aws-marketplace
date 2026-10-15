# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`run_suite`: run a scenario suite against one endpoint and score every session.

Five steps, as the design lists them:
1. Invoke. Every (scenario, repeat) calls `InvokeAgentRuntime` as the eval-runner role, concurrency 12, with
   the envelope `variant=eval, shadow=true` and the identity of the manifest being measured; a multi-turn
   scenario is one runtime session with one invocation per turn. Session IDs are
   `m11-eval-<runId>-<scenario>-<r>-<uuid8>` (33..256 characters). `StopRuntimeSession` follows each session.
   A throttled call halves the concurrency.
2. Wait for spans once, concurrently: one Logs Insights query per poll over every pending session.
3. Fetch each ready session's spans (`aws/spans.py`).
4. Evaluate: one `Evaluate` per evaluator and session, concurrency 16 (`aws/evaluations.py`).
5. Record: the RunRecord, which the caller writes with its own role (the eval-runner role has no S3 access).

An adversarial scenario that the guardrail stops (`status=blocked`) has no tool calls to judge,
so its judged built-in evaluators record a pass labelled BLOCKED_BY_GUARDRAIL; the code-based leakage
evaluator still runs on its spans. The same block on any other category is a real failure and is scored.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from module11.aws import evaluations as EV
from module11.aws import spans as SP
from module11.release import deploy as D
from module11.release import gate as G
from module11.release import eval_names

from module11.runtime import model_retry as MR

from . import control as CT
from . import cost as COST
from . import warmup as WU
from .records import RunRecord, SessionRecord

INVOKE_CONCURRENCY = 12
EVAL_CONCURRENCY = 16
# Transport re-invokes: only for a failure provably before the container ran.
PRE_DISPATCH_CODES = frozenset({"RuntimeClientError", "ServiceUnavailableException"})
MAX_TRANSPORT_REINVOKES = 2
PROOF_DELAY_SECONDS = 180.0        # span and log ingestion lag before "no records" counts as proof
THROTTLE_CODES = {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}
THROTTLE_DETAILS = {"RateLimitError", "ThrottlingException"}
MAX_TURN_ATTEMPTS = 5
PRINCIPAL = "m11-eval"
COMPLETE = ("completed", "blocked")


class MissingEvaluator(RuntimeError):
    """A logical evaluator has no deployed evaluator ID on this stage (the stage was not redeployed)."""


@dataclass(frozen=True)
class Target:
    """What one run measures: the endpoint and the manifest that endpoint serves."""
    stage: str
    endpoint: str
    runtime_arn: str
    runtime_id: str
    manifest_version: str
    behavior_digest: str
    release: str
    evaluator_ids: Mapping[str, str] = field(default_factory=dict)

    @property
    def log_group(self) -> str:
        return eval_names.runtime_log_group(self.runtime_id, self.endpoint)

    @classmethod
    def from_current(cls, current: Mapping[str, Any], stage: str, endpoint: str | None = None) -> "Target":
        dep = current["deployment"][stage]
        return cls(stage, endpoint or eval_names.eval_endpoint(stage), dep["runtimeArn"], dep["runtimeId"],
                   current["manifestVersion"], current["digests"]["behavior"], dep["release"],
                   dict(dep.get("evaluatorIds") or {}))


@dataclass(frozen=True)
class EvaluatorSpec:
    id: str              # logical ID used in thresholds.yaml
    level: str           # session | trace
    kind: str            # builtin | code
    real_id: str         # the ID Evaluate takes


def evaluator_specs(doc: Mapping[str, Any], evaluator_ids: Mapping[str, str],
                    only: Sequence[str] | None = None) -> list[EvaluatorSpec]:
    """evaluators.yaml -> specs; code-based evaluators resolve through the stage's `evaluatorIds` output."""
    out = []
    for entry in doc["evaluators"]:
        if only is not None and entry["id"] not in only:
            continue
        real = entry["id"] if entry["kind"] == "builtin" else evaluator_ids.get(entry["id"])
        if real is None:
            raise MissingEvaluator(f"{entry['id']} has no deployed evaluator on this stage: redeploy it "
                                   "(`module11 release deploy`), which records evaluatorIds")
        out.append(EvaluatorSpec(entry["id"], entry["level"], entry["kind"], real))
    return out


@dataclass
class Clients:
    """The eval-runner session's clients: AgentCore data plane for invoke and Evaluate, and Logs."""
    core: Any            # bedrock-agentcore: invoke_agent_runtime, stop_runtime_session, evaluate
    logs: Any
    dp: Any = None

    def __post_init__(self) -> None:
        self.dp = self.dp or self.core

    @classmethod
    def from_session(cls, session: Any, region: str) -> "Clients":
        from botocore.config import Config
        config = Config(max_pool_connections=64, read_timeout=330, retries={"max_attempts": 3, "mode": "standard"})
        return cls(session.client("bedrock-agentcore", region_name=region, config=config),
                   session.client("logs", region_name=region, config=config))


class Limiter:
    """A concurrency limit that only goes down: a throttled call halves it."""

    def __init__(self, limit: int) -> None:
        self.limit, self.floor, self.active, self.halvings = limit, 1, 0, 0
        self._cv = threading.Condition()

    def __enter__(self) -> "Limiter":
        with self._cv:
            while self.active >= self.limit:
                self._cv.wait()
            self.active += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        with self._cv:
            self.active -= 1
            self._cv.notify_all()

    def halve(self) -> tuple[int, int]:
        with self._cv:
            before = self.limit
            self.limit = max(self.floor, self.limit // 2)
            if self.limit != before:
                self.halvings += 1
            return before, self.limit


def _code(exc: Exception) -> str:
    # botocore's ResponseStreamingError (a connection broken mid-stream) carries response=None.
    return str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")) or type(exc).__name__


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def trace_ids_of(spans: Sequence[Mapping[str, Any]]) -> list[str]:
    """Trace IDs of the session, earliest first; only traces with an invoke_agent span when there are any."""
    agent: set[str] = set()
    first: dict[str, int] = {}
    for span in spans:
        trace = span.get("traceId")
        if not trace:
            continue
        name = str(span.get("name") or "")
        if name.startswith("invoke_agent") or (span.get("attributes") or {}).get("gen_ai.operation.name") == "invoke_agent":
            agent.add(trace)
        stamp = int(span.get("startTimeUnixNano") or span.get("timeUnixNano") or 0)
        if trace not in first or (stamp and (first[trace] == 0 or stamp < first[trace])):
            first[trace] = stamp
    chosen = {t: s for t, s in first.items() if t in agent} or first
    return [t for t, _ in sorted(chosen.items(), key=lambda kv: (kv[1], kv[0]))]


class SuiteRun:
    """One run of a suite. `run()` does all five steps; `run_session` and `evaluate_session` are public
    so the live tests can drive a single scenario."""

    def __init__(self, clients: Clients, target: Target, scenarios: Sequence[Mapping[str, Any]], *,
                 repeats: Mapping[str, int], evaluators: Sequence[EvaluatorSpec], profile: str, scope: str,
                 evaluator_set_version: str, suite: str, kind: str = "gate", run_id: str | None = None,
                 concurrency: int = INVOKE_CONCURRENCY, eval_concurrency: int = EVAL_CONCURRENCY,
                 prices: Mapping[str, Any] | None = None, model_id: str = "",
                 extra: Mapping[str, Any] | None = None, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 first_poll_delay: float = SP.FIRST_POLL_DELAY, poll_interval: float = SP.POLL_INTERVAL,
                 stable_for: float = SP.STABLE_FOR, give_up_after: float = SP.GIVE_UP_AFTER,
                 warmup: int = 0, control: "CT.ControlArm | None" = None, is_control: bool = False) -> None:
        self.c, self.target, self.scenarios = clients, target, list(scenarios)
        self.repeats, self.evaluators, self.profile, self.scope = dict(repeats), list(evaluators), profile, scope
        self.evaluator_set_version, self.suite, self.kind = evaluator_set_version, suite, kind
        self.run_id = run_id or new_run_id()
        self.initial, self.eval_concurrency = concurrency, eval_concurrency
        self.prices, self.model_id, self.extra = prices, model_id, dict(extra or {})
        self.log, self.clock, self.sleep = log, clock, sleep
        self.first_poll_delay, self.poll_interval = first_poll_delay, poll_interval
        self.stable_for, self.give_up_after = stable_for, give_up_after
        self.limiter = Limiter(concurrency)
        self.by_id = {s["scenario_id"]: s for s in self.scenarios}
        self.warmup_sessions = int(warmup)
        self.control, self.is_control = control, is_control
        self.latency_arm = "control"                # set by _latency_arm for the balanced phase's two arms
        self.request_prefix = "ev"

    # ------------------------------------------------------------ 1. invoke
    def repeats_of(self, scenario: Mapping[str, Any]) -> int:
        return int(self.repeats.get(scenario["category"], self.repeats.get("default", 1)))

    def jobs(self) -> list[tuple[Mapping[str, Any], int]]:
        top = max((self.repeats_of(s) for s in self.scenarios), default=0)
        return [(s, r) for r in range(1, top + 1) for s in self.scenarios if r <= self.repeats_of(s)]

    def _envelope(self, scenario: Mapping[str, Any], text: str, conversation: str, turn: int) -> dict:
        t = self.target
        return {"identity": {"team": scenario["team"], "principal": PRINCIPAL},
                "request": {"change_summary": text,
                            "request_id": f"{self.request_prefix}-{self.run_id}-{uuid.uuid4().hex[:8]}-{turn}"},
                "context": {"variant": "eval", "shadow": True, "scenarioId": scenario["scenario_id"],
                            "conversationId": conversation, "manifestVersion": t.manifest_version,
                            "behaviorDigest": t.behavior_digest, "release": t.release}}

    def _invoke_turn(self, session_id: str, envelope: Mapping[str, Any]) -> dict:
        t = self.target
        for attempt in range(1, MAX_TURN_ATTEMPTS + 1):
            try:
                response = self.c.core.invoke_agent_runtime(
                    agentRuntimeArn=t.runtime_arn, qualifier=t.endpoint, runtimeSessionId=session_id,
                    contentType="application/json", accept="text/event-stream",
                    payload=json.dumps(envelope).encode("utf-8"))
                request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
                text = response["response"].read().decode("utf-8", "replace")
            except Exception as exc:
                code = _code(exc)
                if code in THROTTLE_CODES and attempt < MAX_TURN_ATTEMPTS:
                    before, after = self.limiter.halve()
                    self.log(f"throttled ({code}): concurrency {before} -> {after}")
                    self.sleep(min(5.0 * attempt, 30.0))
                    continue
                return {"status": "invoke_error", "detail": code, "body": {}, "requestId": None, "retries": attempt - 1}
            events = D.sse_events(text)
            final = next((data for name, data in reversed(events) if name in {"result", "error"}), None)
            if not isinstance(final, Mapping):
                return {"status": "failed", "detail": "no_result", "body": {}, "requestId": request_id,
                        "retries": attempt - 1}
            return {"status": str(final.get("status") or "failed"),
                    "detail": final.get("detail") or final.get("error"), "body": final, "requestId": request_id,
                    "retries": attempt - 1}
        return {"status": "invoke_error", "detail": "throttled", "body": {}, "requestId": None,
                "retries": MAX_TURN_ATTEMPTS - 1}

    def _stop(self, session_id: str) -> None:
        t = self.target
        try:
            self.c.core.stop_runtime_session(agentRuntimeArn=t.runtime_arn, qualifier=t.endpoint,
                                             runtimeSessionId=session_id)
        except Exception as exc:        # the 60 s idle timeout ends the session anyway
            self.log(f"WARN stop_runtime_session: {_code(exc)}")

    def run_session(self, scenario: Mapping[str, Any], repeat: int) -> SessionRecord:
        """Steps 1a-1b for one (scenario, repeat); holds one slot of the invoke concurrency limit."""
        with self.limiter:
            record = self._session(scenario, repeat)
            if record.status == "failed" and (record.detail in THROTTLE_DETAILS
                                              or MR.detail_class(record.detail) in THROTTLE_DETAILS):
                before, after = self.limiter.halve()
                self.log(f"{record.sessionId}: model throttled; concurrency {before} -> {after}; retrying once")
                record = self._session(scenario, repeat)
                record.retried = True
            return record

    def _session(self, scenario: Mapping[str, Any], repeat: int) -> SessionRecord:
        suffix = uuid.uuid4().hex[:8]
        sid = CT.session_id(self.run_id, scenario["scenario_id"], repeat, suffix, self.latency_arm) \
            if self.is_control else eval_names.session_id_for_eval(self.run_id, scenario["scenario_id"], repeat, suffix)
        record = SessionRecord(scenario["scenario_id"], scenario["category"], list(scenario.get("tags") or []), repeat, sid)
        conversation = f"eval-{self.run_id}-{uuid.uuid4().hex[:8]}"
        record.startedAt = self.clock()
        for index, turn in enumerate(scenario["turns"]):
            began = self.clock()
            result = self._invoke_turn(sid, self._envelope(scenario, turn["input"], conversation, index))
            record.latencyMs += (self.clock() - began) * 1000.0
            record.turns += 1
            record.invokeRetries += int(result.get("retries") or 0)
            body = result["body"]
            usage = body.get("usage") or {}
            record.inputTokens += int(usage.get("input_tokens") or 0)
            record.outputTokens += int(usage.get("output_tokens") or 0)
            events = body.get("events") or []
            record.toolCalls += len(events)
            record.tools += [e.get("tool") for e in events if e.get("status") == "completed" and e.get("tool")]
            record.guardBlocked += sum(1 for e in events if e.get("error") == "guard_blocked")
            for kind, n in ((body.get("m11") or {}).get("modelRetries") or {}).items():
                record.modelRetries[kind] = record.modelRetries.get(kind, 0) + int(n or 0)
            if result.get("requestId"):
                record.requestIds.append(result["requestId"])
            record.status, record.detail = result["status"], result.get("detail")
            if record.status not in COMPLETE:
                break
        record.endedAt = self.clock()
        record.blocked = record.status == "blocked"
        if record.status != "invoke_error":
            self._stop(sid)
        else:
            record.spans = "not_invoked"
        return record

    # ------------------------------------------------------------ 3-4. fetch and evaluate
    def _row(self, record: SessionRecord, spec: EvaluatorSpec, outcome: EV.Outcome) -> dict:
        return {"scenarioId": record.scenarioId, "category": record.category, "tags": record.tags,
                "repeat": record.repeat, "sessionId": record.sessionId, "evaluatorId": spec.id,
                "value": outcome.value, "label": outcome.label, "errorCode": outcome.error_code,
                "explanation": (outcome.explanation or outcome.error_message or "")[:600],
                "inputTokens": outcome.input_tokens, "outputTokens": outcome.output_tokens,
                "attempts": outcome.attempts}

    def _failed_rows(self, record: SessionRecord, code: str) -> list[dict]:
        rows = []
        for spec in self.evaluators:
            if record.blocked and record.category == "adversarial" and spec.kind == "builtin":
                rows.append(self._row(record, spec, EV.blocked_outcome(spec.real_id)))
            else:
                rows.append(self._row(record, spec, EV.Outcome(spec.real_id, None, error_code=code)))
        return rows

    def evaluate_session(self, record: SessionRecord) -> list[dict]:
        """Steps 3-4 for one ready session: fetch its spans, then one Evaluate per evaluator."""
        scenario = self.by_id[record.scenarioId]
        try:
            spans = SP.session_spans(record.sessionId, self.target.log_group, record.startedAt, record.endedAt,
                                     logs=self.c.logs)
        except SP.SpansError as exc:
            record.spans = exc.code
            return self._failed_rows(record, exc.code)
        except Exception as exc:
            record.spans = "spans_fetch_failed"
            self.log(f"{record.sessionId}: span fetch failed: {_code(exc)}")
            return self._failed_rows(record, "spans_fetch_failed")
        record.spans = "ready"
        traces = trace_ids_of(spans)
        record.traceIds = traces
        last = traces[-1] if traces else None            # only the final turn has an expected response
        rows = []
        for spec in self.evaluators:
            if record.blocked and record.category == "adversarial" and spec.kind == "builtin":
                outcome = EV.blocked_outcome(spec.real_id)
            elif spec.level == "trace" and not last:
                outcome = EV.Outcome(spec.real_id, None, error_code="no_trace")
            else:
                request = EV.request_for(spec.real_id, spans, level=spec.level, trace_ids=[last] if last else None,
                                         refs=EV.references(spec.id, scenario, record.sessionId, last))
                outcome = EV.evaluate(self.c.dp, request, sleep=self.sleep)
            rows.append(self._row(record, spec, outcome))
        return rows

    # ------------------------------------------------------------ transport re-invokes
    def never_reached(self, record: SessionRecord) -> tuple[bool, str]:
        """(True, evidence) only when it is provable that the request never reached the container: a pre-dispatch
        error on the first turn, no tool or model call recorded, and no span document or runtime log line for the
        session ID (spans.session_spans searches aws/spans and the runtime log group). Anything that cannot be
        shown is (False, why)."""
        if record.status != "invoke_error" or record.detail not in PRE_DISPATCH_CODES:
            return False, f"not a pre-dispatch error ({record.detail})"
        if record.turns != 1 or record.toolCalls or record.tools or record.inputTokens or record.outputTokens \
                or record.requestIds or any(record.modelRetries.values()):
            return False, "the session recorded runtime activity"
        wait = record.endedAt + PROOF_DELAY_SECONDS - self.clock()
        if wait > 0:                                   # let span and log ingestion catch up first
            self.sleep(wait)
        try:
            SP.session_spans(record.sessionId, self.target.log_group, record.startedAt, record.endedAt,
                             logs=self.c.logs)
        except SP.SpansError as exc:
            if exc.code == "spans_missing":
                return True, "no span document and no runtime log line"
            return False, f"spans check: {exc.code}"
        except Exception as exc:  # noqa: BLE001 - a failed check proves nothing
            return False, f"spans check failed ({_code(exc) or type(exc).__name__})"
        return False, "the session has span documents or runtime log lines"

    def transport_reinvokes(self, sessions: list[SessionRecord], rows: list[dict]) -> dict:
        """At most one re-invoke per scenario session, with the same input and a new runtime session ID, only for
        sessions that provably never reached the container (`never_reached`). Circuit breaker: more than
        MAX_TRANSPORT_REINVOKES candidates, or a re-invoke that also fails, re-invokes nothing more and marks the run
        `tripped` (a service incident, not noise); the missing results keep the gate INCONCLUSIVE."""
        candidates = [s for s in sessions if s.status == "invoke_error" and s.detail in PRE_DISPATCH_CODES]
        out: dict[str, Any] = {"reinvokes": [], "tripped": None}
        if not candidates:
            return out
        if len(candidates) > MAX_TRANSPORT_REINVOKES:
            out["tripped"] = f"{len(candidates)} pre-dispatch failures (limit {MAX_TRANSPORT_REINVOKES})"
            out["skipped"] = [{"sessionId": s.sessionId, "error": s.detail} for s in candidates]
            self.log(f"WARN transport circuit breaker: {out['tripped']}; nothing re-invoked")
            return out
        for original in candidates:
            proven, evidence = self.never_reached(original)
            entry = {"scenarioId": original.scenarioId, "repeat": original.repeat, "originalSessionId":
                     original.sessionId, "error": original.detail, "evidence": evidence, "reinvoked": proven}
            out["reinvokes"].append(entry)
            if not proven:
                self.log(f"  {original.sessionId}: not re-invoked ({evidence})")
                continue
            retry = self._session(self.by_id[original.scenarioId], original.repeat)
            retry.reinvokeOf = original.sessionId
            entry.update(sessionId=retry.sessionId, status=retry.status, detail=retry.detail)
            self.log(f"  transport re-invoke: {original.sessionId} ({original.detail}) -> {retry.sessionId} "
                     f"{retry.status}")
            sessions[sessions.index(original)] = retry
            rows[:] = [r for r in rows if r["sessionId"] != original.sessionId]
            if retry.status == "invoke_error":
                out["tripped"] = f"the re-invoke of {original.sessionId} also failed ({retry.detail})"
                rows += self._failed_rows(retry, "invoke_failed")
                self.log(f"WARN transport circuit breaker: {out['tripped']}")
                break
            rows += self._evaluate_late(retry)
        return out

    def _evaluate_late(self, record: SessionRecord) -> list[dict]:
        """Spans for one re-invoked session, then its evaluations (the main span wait has finished)."""
        def poll() -> dict:
            return SP.readiness_counts(self.c.logs, self.target.log_group, record.sessionId, record.startedAt - 60,
                                       self.clock(), sleep=self.sleep)
        ready, missing = SP.wait_for_spans(
            poll, [record.sessionId], last_invoke_at=record.endedAt, on_ready=lambda sid: None, clock=self.clock,
            sleep=self.sleep, first_poll_delay=self.first_poll_delay, interval=self.poll_interval,
            give_up_after=self.give_up_after, stable_for=self.stable_for, log=self.log)
        if missing:
            record.spans = "spans_missing"
            return self._failed_rows(record, "spans_missing")
        return self.evaluate_session(record)

    # ------------------------------------------------------------ the balanced latency phase
    def _latency_arm(self, clients: Clients, target: Target, arm: str) -> "SuiteRun":
        """One arm of the balanced phase: the same envelope and session code, its own `concurrency` workers and
        limiter, tagged session and request IDs, never evaluated (evaluation/control.py)."""
        run = SuiteRun(clients, target, self.control.scenarios, repeats={"default": self.control.repeats},
                       evaluators=[], profile=self.profile, scope=self.scope,
                       evaluator_set_version=self.evaluator_set_version, suite=self.suite, kind=f"latency-{arm}",
                       run_id=self.run_id, concurrency=self.control.concurrency, log=self.log, clock=self.clock,
                       sleep=self.sleep, is_control=True)
        run.latency_arm = arm
        run.request_prefix = CT.REQUEST_PREFIX[arm]
        return run

    def latency_phase(self) -> dict:
        """After the main suite: CONTROL_REPEATS x the happy_path scenarios on the candidate and on prod `stable`
        at the same time, each arm in its own pool of the same size, so both runtimes carry the same load in the
        same window. Returns the RunRecord's `control` block."""
        arms = {"candidate": self._latency_arm(self.c, self.target, "candidate"),
                "control": self._latency_arm(self.control.clients, self.control.target, "control")}
        warm = WU.run(arms["control"].c.core, self.control.target, self.run_id, self.control.warmup,
                      clock=self.clock, log=self.log) if self.control.warmup else {"sessions": 0}
        jobs = {name: run.jobs() for name, run in arms.items()}
        self.log(f"latency phase {self.run_id}: {len(jobs['candidate'])} candidate sessions on "
                 f"{self.target.stage}/{self.target.endpoint} and {len(jobs['control'])} control sessions on "
                 f"{self.control.target.stage}/{self.control.target.endpoint}, {self.control.concurrency} workers each")
        t0 = self.clock()
        pools = {name: ThreadPoolExecutor(max_workers=self.control.concurrency, thread_name_prefix=f"m11-lat-{name}")
                 for name in arms}
        try:
            futures = {name: [pools[name].submit(arms[name].run_session, s, r) for s, r in jobs[name]]
                       for name in arms}
            done = {name: [f.result() for f in futures[name]] for name in arms}
        finally:
            for pool in pools.values():
                pool.shutdown(wait=True)
        seconds = round(self.clock() - t0, 1)
        rows = {name: sorted((s.__dict__ for s in done[name]), key=lambda d: (d["scenarioId"], d["repeat"]))
                for name in arms}
        cost = sum(float((COST.measure({"sessions": rows[n], "results": []}, self.prices, self.model_id)
                          or {}).get("totalUsd") or 0.0) for n in arms if rows[n])
        t = self.control.target
        self.log(f"latency phase {self.run_id}: {seconds:.0f} s, ${cost:.2f}")
        return {"design": "balanced", "scope": CT.CONTROL_SCOPE, "repeats": self.control.repeats,
                "concurrency": {name: self.control.concurrency for name in arms},
                "target": {"stage": t.stage, "endpoint": t.endpoint, "manifestVersion": t.manifest_version,
                           "release": t.release, "runtimeArn": t.runtime_arn},
                "sessions": len(rows["control"]), "candidateSessions": len(rows["candidate"]),
                "records": rows["control"], "candidateRecords": rows["candidate"], "warmup": warm,
                "seconds": seconds, "costUsd": round(cost, 4)}

    # ------------------------------------------------------------ the whole run
    def run(self) -> RunRecord:
        # Gate warm-up (evaluation/warmup.py): untimed shadow sessions on the new runtime version, before t0, so
        # they are part of no statistic, span query (prefix m11-eval-<runId>-), coverage or cost.
        warm = WU.run(self.c.core, self.target, self.run_id, self.warmup_sessions, clock=self.clock, log=self.log) \
            if self.warmup_sessions else {"sessions": 0}
        t0 = self.clock()
        jobs = self.jobs()
        self.log(f"run {self.run_id}: {len(self.scenarios)} scenarios, {len(jobs)} sessions on "
                 f"{self.target.stage}/{self.target.endpoint} ({self.target.manifest_version}), "
                 f"concurrency {self.initial}")
        sessions: list[SessionRecord] = []
        with ThreadPoolExecutor(max_workers=self.initial, thread_name_prefix="m11-invoke") as pool:
            futures = [pool.submit(self.run_session, s, r) for s, r in jobs]
            for done, future in enumerate(as_completed(futures), start=1):
                sessions.append(future.result())
                if done % 20 == 0 or done == len(futures):
                    self.log(f"  invoked {done}/{len(futures)} sessions ({int(self.clock() - t0)} s)")
        t_invoked = self.clock()
        control = self.latency_phase() if self.control is not None and self.control.scenarios else {}
        last_end = max((s.endedAt for s in sessions), default=t_invoked)
        by_sid = {s.sessionId: s for s in sessions}
        rows: list[dict] = []
        futures_eval: dict[str, Future] = {}
        with ThreadPoolExecutor(max_workers=self.eval_concurrency, thread_name_prefix="m11-eval") as pool:
            for s in sessions:
                if s.status == "invoke_error":
                    rows += self._failed_rows(s, "invoke_failed")

            def on_ready(sid: str) -> None:
                futures_eval[sid] = pool.submit(self.evaluate_session, by_sid[sid])

            pending = [s.sessionId for s in sessions if s.status != "invoke_error"]
            prefix = f"m11-eval-{self.run_id}-"

            def poll() -> dict:
                return SP.readiness_counts(self.c.logs, self.target.log_group, prefix, t0 - 60, self.clock(),
                                           sleep=self.sleep)
            ready, missing = SP.wait_for_spans(
                poll, pending, last_invoke_at=last_end, on_ready=on_ready, clock=self.clock, sleep=self.sleep,
                first_poll_delay=self.first_poll_delay, interval=self.poll_interval, give_up_after=self.give_up_after,
                stable_for=self.stable_for, log=self.log)
            t_ready = self.clock()
            for sid in missing:
                by_sid[sid].spans = "spans_missing"
                rows += self._failed_rows(by_sid[sid], "spans_missing")
            if missing:
                self.log(f"  {len(missing)} sessions have no spans {int(self.give_up_after)} s after the last invocation")
            for sid, future in futures_eval.items():
                rows += future.result()
        transport = self.transport_reinvokes(sessions, rows)
        t_evaluated = self.clock()
        rows.sort(key=lambda r: (r["scenarioId"], r["repeat"], r["evaluatorId"]))
        sessions.sort(key=lambda s: (s.scenarioId, s.repeat))
        record = RunRecord(
            runId=self.run_id, kind=self.kind, stage=self.target.stage, endpoint=self.target.endpoint,
            profile=self.profile, scope=self.scope, repeats=dict(self.repeats),
            manifestVersion=self.target.manifest_version, behaviorDigest=self.target.behavior_digest,
            release=self.target.release, evaluatorSetVersion=self.evaluator_set_version, suiteSha256=self.suite,
            evaluators=[e.id for e in self.evaluators], sessions=[s.__dict__ for s in sessions], results=rows,
            startedAt=t0, finishedAt=t_evaluated,
            timings={"invoke_s": round(t_invoked - t0, 1), "spans_ready_s": round(t_ready - t_invoked, 1),
                     "evaluate_s": round(t_evaluated - t_ready, 1), "total_s": round(t_evaluated - t0, 1)},
            concurrency={"initial": self.initial, "final": self.limiter.limit, "halvings": self.limiter.halvings},
            extra=dict(self.extra), warmup=warm)
        record.transport = transport
        record.cost = COST.measure(record.to_dict(), self.prices, self.model_id)
        record.control = control
        if control:
            record.timings["latency_phase_s"] = control["seconds"]
        self.log(f"run {self.run_id}: {len(sessions)} sessions, {len(rows)} evaluations in "
                 f"{record.timings['total_s']:.0f} s (invoke {record.timings['invoke_s']:.0f}, spans "
                 f"{record.timings['spans_ready_s']:.0f}, evaluate {record.timings['evaluate_s']:.0f}); "
                 f"measured ${record.cost['totalUsd']:.2f}")
        return record


def run_suite(clients: Clients, target: Target, scenarios: Sequence[Mapping[str, Any]], *,
              repeats: Mapping[str, int], evaluators: Sequence[EvaluatorSpec], profile: str, scope: str,
              evaluator_set_version: str, suite: str | None = None, **options: Any) -> RunRecord:
    """Run `scenarios` against `target` and return the RunRecord. `scenarios` are already
    narrowed to the run scope; `repeats` maps category -> r (and `default`)."""
    suite = suite if suite is not None else G.suite_sha256(scenarios)
    return SuiteRun(clients, target, scenarios, repeats=repeats, evaluators=evaluators, profile=profile, scope=scope,
                    evaluator_set_version=evaluator_set_version, suite=suite, **options).run()
