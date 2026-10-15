# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_invoke_agentcore.py
==============================
Offline tests for the Region resolution in scripts/invoke_agentcore.py:
--region, then AWS_REGION, then AWS_DEFAULT_REGION, then the Region in the
runtime ARN, then us-east-1. No AWS call is made.

Run:
    pytest tests/test_invoke_agentcore.py -v
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("boto3")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "invoke_agentcore.py"
_spec = importlib.util.spec_from_file_location("invoke_agentcore", _SCRIPT)
invoke_agentcore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(invoke_agentcore)

ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/demo-agent"


@pytest.fixture(autouse=True)
def no_region_env(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


def test_explicit_region_wins(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert invoke_agentcore.resolve_region(ARN, "ap-south-1") == "ap-south-1"


def test_aws_region_beats_default_region_and_arn(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    assert invoke_agentcore.resolve_region(ARN) == "us-west-2"


def test_aws_default_region_beats_arn(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    assert invoke_agentcore.resolve_region(ARN) == "ca-central-1"


def test_region_parsed_from_arn():
    assert invoke_agentcore.resolve_region(ARN) == "eu-west-1"


@pytest.mark.parametrize("arn", [None, "", "not-an-arn", "arn:aws:lambda:us-west-2:1:function:x"])
def test_falls_back_to_us_east_1(arn):
    assert invoke_agentcore.resolve_region(arn) == "us-east-1"


def test_invoke_builds_client_in_resolved_region(monkeypatch):
    calls = {}

    class FakeClient:
        def invoke_agent_runtime(self, **kwargs):
            calls["kwargs"] = kwargs
            return {"response": [b'{"result": "ok"}']}

    def fake_client(service, region_name=None):
        calls["service"] = service
        calls["region"] = region_name
        return FakeClient()

    monkeypatch.setattr(invoke_agentcore.boto3, "client", fake_client)
    assert invoke_agentcore.invoke(ARN, "hello") == "ok"
    assert calls["service"] == "bedrock-agentcore"
    assert calls["region"] == "eu-west-1"
    assert calls["kwargs"]["agentRuntimeArn"] == ARN
