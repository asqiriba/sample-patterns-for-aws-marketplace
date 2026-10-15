# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-freeze-check: none passes, planned holds with a continuation token,
emergency fails, and anything unreadable fails closed."""
from __future__ import annotations

import json

import pytest

from module11.lambdas.freeze_check import handler as H


class NotFound(Exception):
    response = {"Error": {"Code": "ParameterNotFound"}}


class SSM:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def get_parameter(self, Name):
        if self.error:
            raise self.error
        return {"Parameter": {"Value": self.value}}


class Pipeline:
    def __init__(self):
        self.calls = []

    def put_job_success_result(self, **kw):
        self.calls.append(("success", kw))

    def put_job_failure_result(self, **kw):
        self.calls.append(("failure", kw))


def run(ssm, continuation=None):
    pipeline = Pipeline()
    clients = {"ssm": ssm, "codepipeline": pipeline}
    job = {"id": "job-1", "data": {"continuationToken": continuation} if continuation else {}}
    out = H.handler({"CodePipeline.job": job}, make=lambda name: clients[name])
    return out["verdict"], pipeline.calls


def test_no_freeze_succeeds():
    verdict, calls = run(SSM(json.dumps({"state": "none"})))
    assert verdict == "success" and calls[0][0] == "success" and "continuationToken" not in calls[0][1]
    verdict, _ = run(SSM(error=NotFound()))
    assert verdict == "success"


def test_planned_freeze_holds_with_a_continuation_token_then_lifts():
    verdict, calls = run(SSM(json.dumps({"state": "planned", "reason": "experiment x-0123456789abcdef"})))
    assert verdict == "hold" and calls[0][0] == "success" and calls[0][1]["continuationToken"]
    assert "experiment x-0123456789abcdef" in calls[0][1]["executionDetails"]["summary"]
    verdict, calls = run(SSM(json.dumps({"state": "none"})), continuation="frozen")
    assert verdict == "success" and "lifted" in calls[0][1]["executionDetails"]["summary"]


def test_emergency_freeze_fails_the_execution():
    verdict, calls = run(SSM(json.dumps({"state": "emergency", "reason": "incident"})))
    assert verdict == "failure" and calls == [("failure", {"jobId": "job-1", "failureDetails": {
        "type": "JobFailed", "message": "emergency freeze: incident"}})]


@pytest.mark.parametrize("ssm", [SSM("{not json"), SSM(json.dumps({"state": "thawed"})),
                                 SSM(error=RuntimeError("AccessDenied"))])
def test_unreadable_freeze_fails_closed(ssm):
    verdict, calls = run(ssm)
    assert verdict == "failure" and calls[0][0] == "failure"
    assert "failed closed" in calls[0][1]["failureDetails"]["message"]
