# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Operator sessions: assume a stage deployer or eval-runner role with a source identity (the
operator trust template).

Every role that trusts `operatorPrincipals` requires `RoleSessionName=m11-operator-*` and a source
identity. A source identity, once set, persists through role chaining and cannot be changed, so:
- an IAM-user caller always passes `SourceIdentity=<user name>`;
- an assumed-role caller (including SSO) omits it first, so a session that already carries one
  propagates it; on AccessDenied it retries exactly once with `SourceIdentity=<role-session name>`,
  and a second AccessDenied is raised.
The audit record takes the source identity from the AssumeRole response, which is what CloudTrail
records for every action of the session.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from module11.release import audit

SESSION_PREFIX = "m11-operator-"
CHAINED_MAX_SECONDS = 3600
_SAFE = re.compile(r"[^\w+=,.@-]")


class AssumeDenied(RuntimeError):
    pass


@dataclass
class OperatorSession:
    role_arn: str
    session_name: str
    source_identity: str | None
    credentials: dict = field(repr=False)
    caller_arn: str = ""

    def env(self) -> dict[str, str]:
        """Environment for a subprocess (pulumi): these credentials, no profile."""
        return {"AWS_ACCESS_KEY_ID": self.credentials["AccessKeyId"],
                "AWS_SECRET_ACCESS_KEY": self.credentials["SecretAccessKey"],
                "AWS_SESSION_TOKEN": self.credentials["SessionToken"]}

    def client(self, service: str, **kwargs: Any) -> Any:
        import boto3
        return boto3.client(service, aws_access_key_id=self.credentials["AccessKeyId"],
                            aws_secret_access_key=self.credentials["SecretAccessKey"],
                            aws_session_token=self.credentials["SessionToken"], **kwargs)


def _principal_name(caller_arn: str) -> tuple[str, str]:
    """('user', <name>) or ('assumed-role', <role-session name>)."""
    resource = caller_arn.split(":", 5)[-1]
    if resource.startswith("user/"):
        return "user", resource.rsplit("/", 1)[-1]
    if resource.startswith("assumed-role/"):
        return "assumed-role", resource.rsplit("/", 1)[-1]
    return "other", resource.rsplit("/", 1)[-1]


BUILD_PREFIX = "m11-build-"


def session_name(caller_arn: str) -> str:
    """`m11-operator-<user or session>`; a pipeline build (an m11 CodeBuild role, trusted by role ARN, not by
    the operator template) is named `m11-build-<role>` so the audit trail never calls it an operator."""
    kind, name = _principal_name(caller_arn)
    if kind == "assumed-role":
        role = caller_arn.split(":", 5)[-1].split("/")[1]
        if role.startswith("m11-"):
            return (BUILD_PREFIX + _SAFE.sub("-", role[len("m11-"):]))[:64]
    return (SESSION_PREFIX + _SAFE.sub("-", name))[:64]


def _denied(exc: Exception) -> bool:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in {"AccessDenied", "AccessDeniedException"}


def assume(role_arn: str, *, sts: Any, duration_seconds: int = 3600, purpose: str = "",
           stage: str = "shared", record: Callable[[dict], None] | None = None,
           policy: str | None = None) -> OperatorSession:
    """`policy` is an inline session policy (JSON text): it can only narrow what the role allows. The evaluation
    tests use it to prove a call needs no permission the role was not given (U-12c)."""
    caller = sts.get_caller_identity()["Arn"]
    kind, name = _principal_name(caller)
    if kind == "assumed-role":
        # Role chaining (a CodeBuild role, SSO, an assumed operator role) caps a session at one hour.
        duration_seconds = min(duration_seconds, CHAINED_MAX_SECONDS)
    base = {"RoleArn": role_arn, "RoleSessionName": session_name(caller), "DurationSeconds": duration_seconds}
    if policy:
        base["Policy"] = policy
    identity = _SAFE.sub("-", name)[:64]
    identity = identity if len(identity) >= 2 else f"{identity}-operator"     # STS needs 2-64 characters
    attempts = [{**base, "SourceIdentity": identity}] if kind == "user" else [base, {**base, "SourceIdentity": identity}]
    last: Exception | None = None
    for request in attempts:
        try:
            response = sts.assume_role(**request)
            break
        except Exception as exc:  # botocore ClientError
            if not _denied(exc):
                raise
            last = exc
    else:
        raise AssumeDenied(f"AssumeRole on {role_arn} was denied for {caller}") from last
    session = OperatorSession(role_arn=role_arn, session_name=base["RoleSessionName"],
                              source_identity=response.get("SourceIdentity"), credentials=response["Credentials"],
                              caller_arn=caller)
    entry = audit.ledger_entry("operator-session", stage, time.time(), caller, role=role_arn,
                               sessionName=session.session_name, sourceIdentity=session.source_identity,
                               purpose=purpose, assumedRoleArn=response.get("AssumedRoleUser", {}).get("Arn"))
    if record:
        record(entry)
    return session


def caller_session(session: Any) -> OperatorSession:
    """The caller's own credentials as an OperatorSession, for a build that is its own deployer (m11-preview-infra:
    it assumes no role). Nothing is assumed, so nothing is audited here."""
    from datetime import datetime, timedelta, timezone
    frozen = session.get_credentials().get_frozen_credentials()
    caller = session.client("sts").get_caller_identity()["Arn"]
    return OperatorSession(role_arn=caller, session_name=caller.rsplit("/", 1)[-1], source_identity=None,
                           credentials={"AccessKeyId": frozen.access_key, "SecretAccessKey": frozen.secret_key,
                                        "SessionToken": frozen.token or "",
                                        "Expiration": datetime.now(timezone.utc) + timedelta(minutes=50)},
                           caller_arn=caller)


def local_record(directory: Path) -> Callable[[dict], None]:
    """Write audit records to a git-ignored local directory (module11/.local/audit)."""
    def write(entry: dict) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"operator-{audit.stamp(time.time())}.json").write_text(json.dumps(entry, indent=2), encoding="utf-8")
    return write
