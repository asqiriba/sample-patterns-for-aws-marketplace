# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MR preview gate reaches its mr-<iid> stage through m11-preview-deployer: the
deployer session reads mr-<iid>/current and assumes the stack's eval runner, so m11-preview needs no mr-*
parameter grant and the eval runner's trust is unchanged. Other stages keep the caller's own session."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from module11.aws import deploy as AD
from module11.aws import operator
from module11 import cli_eval, cli_preview

ACCOUNT = "111122223333"
CREDS = {"AccessKeyId": "AKIA", "SecretAccessKey": "s", "SessionToken": "t",
         "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)}


class FakeSTS:
    def __init__(self, owner, calls):
        self.owner, self.calls = owner, calls

    def get_caller_identity(self):
        return {"Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{self.owner}/build", "Account": ACCOUNT}

    def assume_role(self, **request):
        self.calls.append((self.owner, request["RoleArn"].rsplit("/", 1)[-1]))
        role = request["RoleArn"].rsplit("/", 1)[-1]
        return {"Credentials": {**CREDS, "AccessKeyId": f"AKIA-{role}"},
                "AssumedRoleUser": {"Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{role}/x"}}


class FakeSSM:
    def __init__(self, owner, reads):
        self.owner, self.reads = owner, reads

    def get_parameter(self, Name):
        self.reads.append((self.owner, Name))
        raise RuntimeError("stop after the read")


class Admin:
    """The caller's own session (m11-preview in the build)."""

    def __init__(self, calls, reads):
        self.calls, self.reads = calls, reads

    def client(self, service, **_):
        return FakeSTS("m11-preview", self.calls) if service == "sts" else FakeSSM("m11-preview", self.reads)


def _ctx(monkeypatch, tmp_path: Path):
    calls, reads = [], []

    def client(self, service, **_):           # an assumed session's clients name the role they belong to
        role = self.role_arn.rsplit("/", 1)[-1]
        return FakeSTS(role, calls) if service == "sts" else FakeSSM(role, reads)

    monkeypatch.setattr(operator.OperatorSession, "client", client)
    ctx = AD.Context(root=tmp_path, account=ACCOUNT, region="us-east-1", operators=[], log=lambda _: None,
                     admin=Admin(calls, reads))
    return ctx, calls, reads


def test_the_mr_gate_assumes_its_eval_runner_through_the_preview_deployer(monkeypatch, tmp_path):
    ctx, calls, _ = _ctx(monkeypatch, tmp_path)
    cli_preview.chain_through_deployer(ctx, "mr-7")
    ctx.assume("m11-mr-7-eval-runner", "mr-7", "evaluation run")
    assert calls == [("m11-preview", "m11-preview-deployer"), ("m11-preview-deployer", "m11-mr-7-eval-runner")]


def test_the_deployer_itself_and_other_stages_use_the_callers_session(monkeypatch, tmp_path):
    ctx, calls, _ = _ctx(monkeypatch, tmp_path)
    cli_preview.chain_through_deployer(ctx, "mr-7")
    ctx.sessions.clear()
    ctx.assume("m11-preview-deployer", "mr-7", "deploy")              # not chained through itself
    ctx.assume("m11-prod-eval-runner", "prod", "latency control")     # the control arm: m11-preview's own grant
    assert calls[-2:] == [("m11-preview", "m11-preview-deployer"), ("m11-preview", "m11-prod-eval-runner")]


def test_the_gate_reads_the_mr_stages_current_as_the_deployer(monkeypatch, tmp_path):
    ctx, _, reads = _ctx(monkeypatch, tmp_path)
    cli_preview.chain_through_deployer(ctx, "mr-7")
    for stage, reader in (("mr-7", "m11-preview-deployer"), ("prod", "m11-preview")):
        try:
            cli_eval.target_of(ctx, stage)
        except RuntimeError:
            pass
        assert reads[-1] == (reader, f"/m11/devops-companion/{stage}/current")
