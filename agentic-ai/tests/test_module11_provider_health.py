# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The gate report's model provider health block: informational, from the run's own sessions and
CloudWatch AWS/Bedrock; it never changes a verdict, retries or excludes a session."""
from __future__ import annotations

from botocore.exceptions import ClientError

from module11.evaluation import provider_health as PH
from module11.release import gate as G

T0 = 1_791_418_100.0     # 2026-10-08T00:08:20Z


def _s(status="completed", detail=None, latency=13000.0, start=T0, end=T0 + 20, **kw):
    return {"scenarioId": "incident-triage-001", "repeat": 1, "status": status, "detail": detail,
            "latencyMs": latency, "startedAt": start, "endedAt": end, **kw}


SESSIONS = [_s(), _s(latency=15000.0), _s(latency=50000.0, end=T0 + 450),
            _s("failed", "InternalServerError", 20000.0), _s("failed", "InternalServerError", 18000.0),
            _s("failed", "RateLimitError", 9000.0, retried=True), _s("blocked"),
            _s("failed", "DevOps Companion finished without the required evidence checks"),
            _s(invokeRetries=2)]


class FakeCloudWatch:
    def __init__(self, fail: bool = False):
        self.fail, self.calls = fail, []

    def __init__(self, fail: bool = False, values: dict | None = None, p99: float | None = 17300.0):
        self.fail, self.calls, self.p99 = fail, [], p99
        self.values = values or {"Invocations": [300.0, 135.0], "InvocationServerErrors": [5.0],
                                 "InvocationThrottles": [], "InvocationLatency": [18344.0, 29542.0]}

    def get_metric_statistics(self, **kw):
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetMetricStatistics")
        self.calls.append(kw)
        if "ExtendedStatistics" in kw:
            return {"Datapoints": [] if self.p99 is None else [{"ExtendedStatistics": {"p99": self.p99}}]}
        stat = kw["Statistics"][0]
        return {"Datapoints": [{stat: v} for v in self.values[kw["MetricName"]]]}


def test_errors_are_classified_throttle_versus_5xx():
    assert PH.classify("RateLimitError") == "throttle" and PH.classify("ThrottlingException") == "throttle"
    assert PH.classify("InternalServerError") == "5xx" and PH.classify("ServiceUnavailableException") == "5xx"
    assert PH.classify("DevOps Companion finished without the required evidence checks") is None
    counts = PH.session_counts(SESSIONS)
    assert counts["throttled"] == 1 and counts["serverErrors"] == 2
    assert counts["byError"] == {"RateLimitError": 1, "InternalServerError": 2}
    assert counts["retries"] == 3                        # one re-run session + two re-sent invokes
    assert counts["medianLatencyMs"] == 14000.0          # completed sessions only


def test_runtime_model_retries_and_its_failure_detail_are_counted():
    rows = [_s(modelRetries={"5xx": 2, "throttle": 0, "stream": 0}),
            _s("failed", "DevOps Companion: the model provider failed (InternalServerError HTTP 500) after 2 retries",
               modelRetries={"5xx": 2, "throttle": 0, "stream": 0}),
            _s("failed", "Recovery Planner: the model provider failed (APIConnectionError) after 1 retries",
               modelRetries={"5xx": 0, "throttle": 0, "stream": 1})]
    counts = PH.session_counts(rows)
    assert counts["serverErrors"] == 1 and counts["streamErrors"] == 1
    assert counts["byError"] == {"InternalServerError": 1, "APIConnectionError": 1}
    assert counts["modelRetries"] == {"5xx": 4, "throttle": 0, "stream": 1} and counts["retries"] == 5
    text = PH.markdown({**counts, "window": None, "models": {}})
    assert "1 on a provider 5xx, 1 on a connection error (APIConnectionError 1, InternalServerError 1) after retries" in text
    assert "model-call retries 5 (5xx 4, throttle 0, connection 1), runner retries 0" in text
    old = PH.markdown({"retries": 2, "window": None, "models": {}})
    assert "2 retries (the runtime's model client did not retry)" in old


def test_metrics_cover_the_invoke_window_per_model():
    cw = FakeCloudWatch()
    doc = PH.collect(cw, ["us.openai.gpt-5.6-luna", "us.openai.gpt-5.6-luna"], SESSIONS)
    assert doc["window"] == ["2026-10-08T00:08:00Z", "2026-10-08T00:16:00Z"]          # whole minutes
    assert {c["Dimensions"][0]["Value"] for c in cw.calls} == {"us.openai.gpt-5.6-luna"}  # deduplicated
    assert all(c["Namespace"] == "AWS/Bedrock" for c in cw.calls)
    assert all(c["Period"] == 60 for c in cw.calls if "Statistics" in c)
    pre = doc["preRun"]                                   # the 30 minutes before the first session
    assert pre["window"] == ["2026-10-07T23:38:00Z", "2026-10-08T00:08:00Z"] and pre["healthy"] is False
    assert [c["Period"] for c in cw.calls if "ExtendedStatistics" in c] == [1800]
    assert doc["models"]["us.openai.gpt-5.6-luna"] == {"invocations": 435.0, "throttles": 0.0, "serverErrors": 5.0,
                                                        "maxLatencyMs": 29542.0}
    text = PH.markdown(doc)
    assert text.startswith("**Model provider health (informational, not scored):**")
    assert "1 ended on a model throttle, 2 on a provider 5xx (InternalServerError 2, RateLimitError 1)" in text
    assert ("model-call retries 0 (5xx 0, throttle 0, connection 0), runner retries 3" in text
            and "session latency median 14.0 s" in text)
    assert ("Bedrock us.openai.gpt-5.6-luna 00:08-00:16 UTC, account-wide: 435 invocations, 0 throttles, "
            "5 server errors, max latency 29.5 s.") in text


def test_the_pre_run_reading_is_healthy_only_inside_the_passing_range():
    quiet = {"Invocations": [700.0], "InvocationServerErrors": [], "InvocationThrottles": []}
    ok = PH.reading(FakeCloudWatch(values=quiet, p99=6600.0), ["m"], T0)
    assert ok["healthy"] is True and ok["models"]["m"] == {"invocations": 700.0, "serverErrors": 0.0,
                                                           "throttles": 0.0, "p99LatencyMs": 6600.0}
    assert "m 700 invocations, p99 6.6 s, 0 5xx, 0 throttles: healthy" in PH.reading_markdown(ok)
    slow = PH.reading(FakeCloudWatch(values=quiet, p99=15000.0), ["m"], T0)
    assert slow["healthy"] is False and "p99 15.0 s > 7 s" in slow["why"]
    errors = PH.reading(FakeCloudWatch(values={**quiet, "InvocationServerErrors": [1.0]}, p99=5000.0), ["m"], T0)
    assert errors["healthy"] is False and "1 provider 5xx" in errors["why"]
    empty = PH.reading(FakeCloudWatch(values={**quiet, "Invocations": [12.0]}, p99=2000.0), ["m"], T0)
    assert empty["healthy"] is False and "too few for a p99 (use --probe)" in empty["why"]
    assert PH.reading(FakeCloudWatch(fail=True), ["m"], T0)["healthy"] is False


def test_the_probe_measures_client_latency_and_error_classes():
    class Runtime:
        def __init__(self):
            self.n = 0

        def converse(self, **kw):
            assert kw["modelId"] == "m" and kw["inferenceConfig"] == {"maxTokens": 300}
            self.n += 1
            if self.n % 10 == 0:
                raise ClientError({"Error": {"Code": "InternalServerException", "Message": "x"}}, "Converse")

    ticks = iter(float(i) for i in range(1000))
    result = PH.probe(Runtime(), "m", 20, concurrency=1, clock=lambda: next(ticks))
    assert result["calls"] == 20 and result["ok"] == 18 and result["errors"] == {"InternalServerException": 2}
    assert result["p50Ms"] == 1000.0 and result["concurrency"] == 1


def test_a_failed_metrics_read_is_reported_not_raised():
    doc = PH.collect(FakeCloudWatch(fail=True), ["us.openai.gpt-5.6-luna"], SESSIONS)
    assert doc["models"] == {"error": "AccessDenied"} and "unavailable (AccessDenied)" in PH.markdown(doc)
    assert "unavailable (not read)" in PH.markdown(PH.collect(None, ["m"], SESSIONS))
    assert PH.markdown(None).endswith("not collected.")
    assert PH.collect(FakeCloudWatch(), ["m"], [])["window"] is None


def test_the_block_is_in_the_report_and_never_changes_the_verdict():
    from module11.cli_eval import _report_dict
    doc = PH.collect(FakeCloudWatch(), ["us.openai.gpt-5.6-luna"], SESSIONS)
    for verdict in ("PASS", "FAIL"):
        report = G.GateReport(verdict=verdict, scope="full", comparable=True, reasons=[], lines=[])
        stored = _report_dict(report, {"sessions": 0}, SESSIONS, doc)
        assert stored["verdict"] == verdict and stored["providerHealthData"] == doc
        assert stored["markdown"].endswith(stored["providerHealth"]) and "5 server errors" in stored["markdown"]


def test_model_ids_come_from_models_yaml(tmp_path):
    from module11.cli_eval import model_ids_of
    path = tmp_path / "module11/harness/agent/models.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("companion: {modelId: a}\nplanner: {modelId: a}\nreviewer: {modelId: b}\n", encoding="utf-8")
    assert model_ids_of(tmp_path) == ["a", "b"]
