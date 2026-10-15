# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Model provider health for one gate run: informational, never scored.

A reviewer reading a failed latency or quality row needs to tell a provider incident from a regression. This
block puts the evidence next to the gate table:

- From the run's own sessions: sessions that ended on a model throttle (429) or a provider 5xx, by error class
  (the runtime reports `type(exc).__name__` of the model client's error), and the retries the runner made.
  The runtime's model client does not retry (max_retries=0), so these are all the retries there were.
- From CloudWatch `AWS/Bedrock`, per model ID in models.yaml, over the run's invoke window: invocations,
  throttles, server errors and the maximum invocation latency. These are account-wide for the model, so they
  include callers outside the gate.

- The pre-run reading: the same AWS/Bedrock numbers, with the model's p99 latency, for the 30 minutes before the
  run's first session. This is the window the operator reads before starting a full-scope gate.

Nothing here changes a verdict, retries a session or excludes one. A metrics read that fails is reported as
unavailable and the gate carries on.

Operator scheduling is the interim practice until the latency budget
becomes relative. Before starting a full-scope gate, run `module11 pipeline provider-health`. Start
only when `assess()` says healthy: p99 ≤ 7 s and 0 provider 5xx in the prior 30 minutes, which is the range of
the passing runs. With little traffic, `--probe N` sends N small model calls first so that the window has enough
invocations for a p99. The command records its reading in the release ledger. The gate itself is unchanged.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from module11.runtime import model_retry as MR

# The runtime's model client is the OpenAI SDK on Bedrock (RateLimitError = 429, InternalServerError = 5xx);
# the Bedrock SDK names are listed too so a guardrail or Converse error classifies the same way.
THROTTLES = frozenset({"RateLimitError", "ThrottlingException", "TooManyRequestsException",
                       "ServiceQuotaExceededException", "throttled"})
SERVER_ERRORS = frozenset({"InternalServerError", "InternalServerException", "ServiceUnavailableError",
                           "ServiceUnavailableException", "ModelErrorException", "ModelNotReadyException"})
METRICS = (("invocations", "Invocations", "Sum"), ("throttles", "InvocationThrottles", "Sum"),
           ("serverErrors", "InvocationServerErrors", "Sum"), ("maxLatencyMs", "InvocationLatency", "Maximum"))
NAMESPACE = "AWS/Bedrock"
READING_MINUTES = 30
HEALTHY_P99_MS = 7000.0          # the passing runs' range on 2026-10-07: model p99 4.2-6.6 s
HEALTHY_SERVER_ERRORS = 0
MIN_INVOCATIONS = 100            # fewer calls than this give no meaningful p99


STREAM_ERRORS = frozenset({"APIConnectionError", "APITimeoutError", "ModelRetryStopped"})


def _error_name(detail: str | None) -> str | None:
    """The error class of a session's failure detail: the detail itself (older runtimes) or the class named in
    the runtime's `... the model provider failed (<Class> ...` message (runtime/model_retry.py)."""
    if detail in THROTTLES or detail in SERVER_ERRORS or detail in STREAM_ERRORS:
        return detail
    return MR.detail_class(detail)


def classify(detail: str | None) -> str | None:
    """'throttle', '5xx', 'stream' or None for a session's failure detail."""
    name = _error_name(detail)
    if name in THROTTLES:
        return "throttle"
    if name in SERVER_ERRORS:
        return "5xx"
    if name in STREAM_ERRORS:
        return "stream"
    return None


def session_counts(sessions: Iterable[Mapping[str, Any]]) -> dict:
    """Provider errors and retries in the run's own sessions."""
    sessions = list(sessions)
    by_class: dict[str, dict[str, int]] = {"throttle": {}, "5xx": {}, "stream": {}}
    for s in sessions:
        kind = classify(s.get("detail"))
        if kind and s.get("status") not in ("completed", "blocked"):
            name = _error_name(s.get("detail")) or str(s.get("detail"))
            by_class[kind][name] = by_class[kind].get(name, 0) + 1
    model = {c: 0 for c in MR.CLASSES}
    for s in sessions:
        for kind, n in (s.get("modelRetries") or {}).items():
            model[kind] = model.get(kind, 0) + int(n or 0)
    runner = sum(1 for s in sessions if s.get("retried")) + sum(int(s.get("invokeRetries") or 0) for s in sessions)
    latencies = [float(s["latencyMs"]) for s in sessions if s.get("status") == "completed" and s.get("latencyMs")]
    return {"sessions": len(sessions),
            "throttled": sum(by_class["throttle"].values()), "serverErrors": sum(by_class["5xx"].values()),
            "streamErrors": sum(by_class["stream"].values()),
            "byError": {**by_class["throttle"], **by_class["5xx"], **by_class["stream"]},
            "modelRetries": model, "runnerRetries": runner, "retries": runner + sum(model.values()),
            "medianLatencyMs": round(statistics.median(latencies), 1) if latencies else None}


def window(sessions: Iterable[Mapping[str, Any]]) -> tuple[float, float] | None:
    """The invoke window, whole minutes (CloudWatch's 60 s period)."""
    spans = [(float(s["startedAt"]), float(s["endedAt"])) for s in sessions if s.get("startedAt") and s.get("endedAt")]
    if not spans:
        return None
    start, end = min(a for a, _ in spans), max(b for _, b in spans)
    return float(int(start // 60) * 60), float(int(-(-end // 60)) * 60)


def model_metrics(cloudwatch: Any, model_ids: Iterable[str], span: tuple[float, float]) -> dict:
    """{modelId: {invocations, throttles, serverErrors, maxLatencyMs}} or {"error": code} if the read fails."""
    start, end = (datetime.fromtimestamp(t, timezone.utc) for t in span)
    out: dict[str, Any] = {}
    try:
        for model in dict.fromkeys(m for m in model_ids if m):
            row: dict[str, float | None] = {}
            for key, name, stat in METRICS:
                points = cloudwatch.get_metric_statistics(
                    Namespace=NAMESPACE, MetricName=name, Dimensions=[{"Name": "ModelId", "Value": model}],
                    StartTime=start, EndTime=end, Period=60, Statistics=[stat]).get("Datapoints") or []
                values = [float(p[stat]) for p in points if stat in p]
                row[key] = (max(values) if stat == "Maximum" else sum(values)) if values else (None if stat == "Maximum" else 0.0)
            out[model] = row
    except Exception as exc:  # noqa: BLE001 - informational: report and carry on
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        return {"error": code or type(exc).__name__}
    return out


def reading(cloudwatch: Any, model_ids: Iterable[str], end: float, minutes: int = READING_MINUTES) -> dict:
    """The prior-`minutes` AWS/Bedrock reading per model: invocations, server errors, throttles and p99 latency,
    with `assess()`'s verdict. A failed read is unhealthy ("metrics unavailable")."""
    start = end - minutes * 60
    t0, t1 = (datetime.fromtimestamp(t, timezone.utc) for t in (start, end))
    doc: dict[str, Any] = {"window": [_iso(start), _iso(end)], "minutes": minutes, "models": {}}
    try:
        for model in dict.fromkeys(m for m in model_ids if m):
            dims = [{"Name": "ModelId", "Value": model}]
            row: dict[str, float | None] = {}
            for key, name in (("invocations", "Invocations"), ("serverErrors", "InvocationServerErrors"),
                              ("throttles", "InvocationThrottles")):
                points = cloudwatch.get_metric_statistics(Namespace=NAMESPACE, MetricName=name, Dimensions=dims,
                                                          StartTime=t0, EndTime=t1, Period=60,
                                                          Statistics=["Sum"]).get("Datapoints") or []
                row[key] = sum(float(p["Sum"]) for p in points if "Sum" in p)
            points = cloudwatch.get_metric_statistics(Namespace=NAMESPACE, MetricName="InvocationLatency",
                                                      Dimensions=dims, StartTime=t0, EndTime=t1, Period=minutes * 60,
                                                      ExtendedStatistics=["p99"]).get("Datapoints") or []
            p99s = [float(p["ExtendedStatistics"]["p99"]) for p in points if "p99" in (p.get("ExtendedStatistics") or {})]
            row["p99LatencyMs"] = max(p99s) if p99s else None     # the larger one if the window spans two periods
            doc["models"][model] = row
    except Exception as exc:  # noqa: BLE001 - informational: report and carry on
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        doc["models"] = {"error": code or type(exc).__name__}
    doc["healthy"], doc["why"] = assess(doc["models"], minutes)
    return doc


def assess(models: Mapping[str, Any], minutes: int = READING_MINUTES) -> tuple[bool, str]:
    """Operator scheduling: healthy only inside the passing runs' range (p99 ≤ 7 s, 0 provider 5xx)."""
    if not models or "error" in models:
        return False, f"metrics unavailable ({(models or {}).get('error', 'no model')})"
    for model, m in models.items():
        if (m.get("invocations") or 0) < MIN_INVOCATIONS:
            return False, (f"{model}: {int(m.get('invocations') or 0)} invocations in {minutes} min, too few for a "
                           f"p99 (use --probe)")
        if (m.get("serverErrors") or 0) > HEALTHY_SERVER_ERRORS:
            return False, f"{model}: {int(m['serverErrors'])} provider 5xx in {minutes} min"
        if m.get("p99LatencyMs") is None or m["p99LatencyMs"] > HEALTHY_P99_MS:
            p99 = m.get("p99LatencyMs")
            return False, f"{model}: p99 {'n/a' if p99 is None else f'{p99 / 1000:.1f} s'} > {HEALTHY_P99_MS / 1000:.0f} s"
    return True, f"within the passing runs' range (p99 ≤ {HEALTHY_P99_MS / 1000:.0f} s, 0 provider 5xx)"


def reading_markdown(doc: Mapping[str, Any]) -> str:
    models = doc.get("models") or {}
    when = f"{doc['window'][0][11:16]}-{doc['window'][1][11:16]} UTC"
    if "error" in models:
        rows = f"unavailable ({models['error']})"
    else:
        parts = []
        for model, m in models.items():
            p99 = m.get("p99LatencyMs")
            parts.append(f"{model} {int(m.get('invocations') or 0)} invocations, p99 "
                         f"{'n/a' if p99 is None else f'{p99 / 1000:.1f} s'}, {int(m.get('serverErrors') or 0)} 5xx, "
                         f"{int(m.get('throttles') or 0)} throttles")
        rows = "; ".join(parts)
    verdict = "healthy" if doc.get("healthy") else "not healthy"
    return f"Pre-run reading ({doc.get('minutes', READING_MINUTES)} min, {when}): {rows}: {verdict} ({doc.get('why')})."


def probe(runtime: Any, model_id: str, calls: int, concurrency: int = 12, clock=time.monotonic) -> dict:
    """`calls` small Converse requests (about 1,500 input tokens, ≤ 300 output) at the gate's concurrency, so a
    quiet window has enough invocations for a p99. Client-side latencies and error classes are returned."""
    from concurrent.futures import ThreadPoolExecutor
    text = "\n".join(f"{i:02d} checkout-api p95 2.4 s after deploy d-4471; payments-db connections 480/500; "
                     f"error rate 3.1%; runbook RB-12 suggests checking the connection pool." for i in range(60))
    prompt = f"Incident notes:\n{text}\n\nSummarize the incident in about 80 words."

    def one(_: int) -> tuple[float, str | None]:
        began = clock()
        try:
            runtime.converse(modelId=model_id, messages=[{"role": "user", "content": [{"text": prompt}]}],
                             inferenceConfig={"maxTokens": 300})
            return (clock() - began) * 1000.0, None
        except Exception as exc:  # noqa: BLE001 - the error class is the measurement
            code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
            return (clock() - began) * 1000.0, code or type(exc).__name__

    with ThreadPoolExecutor(max(1, min(concurrency, calls))) as pool:
        results = list(pool.map(one, range(calls)))
    ok = sorted(ms for ms, err in results if err is None)
    errors: dict[str, int] = {}
    for _, err in results:
        if err:
            errors[err] = errors.get(err, 0) + 1
    pct = (lambda q: round(ok[min(len(ok) - 1, int(q * len(ok)))], 1) if ok else None)
    return {"model": model_id, "calls": calls, "concurrency": min(concurrency, calls), "ok": len(ok),
            "errors": errors, "p50Ms": pct(0.50), "p99Ms": pct(0.99), "maxMs": round(ok[-1], 1) if ok else None}


def transport_markdown(transport: Mapping[str, Any] | None) -> str:
    """The transport re-invokes (runner.transport_reinvokes): each original session, its error and evidence."""
    transport = transport or {}
    rows = transport.get("reinvokes") or []
    done = [r for r in rows if r.get("reinvoked")]
    head = f"**Transport re-invokes:** {len(done)}"
    parts = [f"{r['scenarioId']}#{r['repeat']} {r['originalSessionId']} ({r['error']}; {r['evidence']})"
             + (f" -> {r['sessionId']} {r.get('status')}" if r.get("reinvoked") else ": not re-invoked")
             for r in rows]
    parts += [f"{r['sessionId']} ({r['error']}): not re-invoked" for r in transport.get("skipped") or []]
    text = head + (": " + "; ".join(parts) if parts else "") + "."
    if transport.get("tripped"):
        text += f" Circuit breaker tripped: {transport['tripped']} (a service incident; the gate stays INCONCLUSIVE)."
    return text


def collect(cloudwatch: Any, model_ids: Iterable[str], sessions: Iterable[Mapping[str, Any]],
            transport: Mapping[str, Any] | None = None) -> dict:
    sessions = list(sessions)
    model_ids = list(model_ids)
    span = window(sessions)
    doc: dict[str, Any] = {**session_counts(sessions), "window": None, "models": {}, "preRun": None,
                           "transport": dict(transport or {})}
    if span:
        doc["window"] = [_iso(span[0]), _iso(span[1])]
        doc["models"] = model_metrics(cloudwatch, model_ids, span) if cloudwatch is not None else {"error": "not read"}
        if cloudwatch is not None:
            doc["preRun"] = reading(cloudwatch, model_ids, span[0])
    return doc


def markdown(doc: Mapping[str, Any] | None) -> str:
    head = "**Model provider health (informational, not scored):**"
    if not doc:
        return f"{head} not collected."
    by = ", ".join(f"{k} {v}" for k, v in sorted((doc.get("byError") or {}).items()))
    median = doc.get("medianLatencyMs")
    model = doc.get("modelRetries")
    if model is None:                              # a record from before the runtime retried model calls
        retries = f"{doc.get('retries', 0)} retries (the runtime's model client did not retry)"
    else:
        retries = (f"model-call retries {sum(model.values())} (5xx {model.get('5xx', 0)}, throttle "
                   f"{model.get('throttle', 0)}, connection {model.get('stream', 0)}), runner retries "
                   f"{doc.get('runnerRetries', 0)}")
    stream = f", {doc['streamErrors']} on a connection error" if doc.get("streamErrors") else ""
    parts = [f"gate sessions: {doc.get('throttled', 0)} ended on a model throttle, "
             f"{doc.get('serverErrors', 0)} on a provider 5xx{stream}" + (f" ({by})" if by else "") +
             f" after retries; {retries}" +
             (f"; session latency median {median / 1000:.1f} s" if median else "") + "."]
    models = doc.get("models") or {}
    if doc.get("window"):
        when = f"{doc['window'][0][11:16]}-{doc['window'][1][11:16]} UTC"
        if "error" in models:
            parts.append(f"Bedrock metrics {when}: unavailable ({models['error']}).")
        for model, m in models.items() if "error" not in models else ():
            latency = m.get("maxLatencyMs")
            parts.append(f"Bedrock {model} {when}, account-wide: {int(m.get('invocations') or 0)} invocations, "
                         f"{int(m.get('throttles') or 0)} throttles, {int(m.get('serverErrors') or 0)} server errors, "
                         f"max latency {'n/a' if latency is None else f'{latency / 1000:.1f} s'}.")
    if doc.get("preRun"):
        parts.append(reading_markdown(doc["preRun"]))
    if (doc.get("transport") or {}).get("reinvokes") or (doc.get("transport") or {}).get("tripped"):
        parts.append(transport_markdown(doc["transport"]).replace("**", ""))
    parts.append("Provider errors with an unchanged median point to the provider, not the release; "
                 "the verdict does not use these numbers.")
    return f"{head} " + " ".join(parts)


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
