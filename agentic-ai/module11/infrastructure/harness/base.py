# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared context and small helpers for the m11-harness program modules."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import pulumi
import pulumi_aws as aws

from module11.release import naming
from module11.release.drift import policy_digest

from .config_model import StageSettings

LOG_RETENTION_DAYS = 30


@dataclass
class Stage:
    settings: StageSettings
    account: str
    region: str
    release_bucket: str
    boundary_arn: str | None            # set for mr-* stacks (P8)
    partition: str = "aws"
    policy_docs: dict[str, pulumi.Output] = field(default_factory=dict)   # role name -> rendered policy

    @property
    def name(self) -> str:
        return self.settings.stage

    @property
    def key(self) -> str:
        return self.settings.stagekey

    @property
    def tags(self) -> dict[str, str]:
        return {"Project": naming.PROJECT_TAG, "Stage": self.name}

    def arn(self, service: str, resource: str) -> str:
        return f"arn:{self.partition}:{service}:{self.region}:{self.account}:{resource}"

    def s3(self, bucket: str, key: str | None = None) -> str:
        return f"arn:{self.partition}:s3:::{bucket}" + (f"/{key}" if key is not None else "")

    def log_group_arn(self, name: str) -> str:
        return self.arn("logs", f"log-group:{name}")

    @property
    def artifacts_bucket(self) -> str:
        return naming.artifacts_bucket(self.name, self.account, self.region)

    def role(self, logical: str, name: str, service: str, *, extra_tags: Mapping[str, str] | None = None,
             source_arn: str | None = None) -> aws.iam.Role:
        """A service role whose trust carries aws:SourceAccount (P6); mr-* roles get the boundary (P8)."""
        condition: dict[str, Any] = {"StringEquals": {"aws:SourceAccount": self.account}}
        if source_arn:
            condition["ArnLike"] = {"aws:SourceArn": source_arn}
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                         "Action": "sts:AssumeRole", "Condition": condition}]}
        return aws.iam.Role(logical, name=name, assume_role_policy=json.dumps(trust),
                            permissions_boundary=self.boundary_arn, tags={**self.tags, **(extra_tags or {})})

    def role_policy(self, logical: str, role_name: str, statements: pulumi.Input[Sequence[dict]],
                    *, opts: pulumi.ResourceOptions | None = None) -> aws.iam.RolePolicy:
        document = pulumi.Output.from_input(statements).apply(
            lambda s: json.dumps({"Version": "2012-10-17", "Statement": [x for x in s if x]}, sort_keys=True))
        self.policy_docs[f"{role_name}/{logical}"] = document
        return aws.iam.RolePolicy(logical, name=logical, role=role_name, policy=document, opts=opts)

    def log_group(self, logical: str, name: pulumi.Input[str]) -> aws.cloudwatch.LogGroup:
        return aws.cloudwatch.LogGroup(logical, name=name, retention_in_days=LOG_RETENTION_DAYS, tags=self.tags)

    def digests(self) -> pulumi.Output:
        """`policyDigests{role/policy: sha256}` for /applied, which lets drift watch IAM."""
        keys = sorted(self.policy_docs)
        return pulumi.Output.all(*[self.policy_docs[k] for k in keys]).apply(
            lambda docs: {k: policy_digest(d) for k, d in zip(keys, docs)})


def lambda_logs_statement(stage: Stage, function_name: str) -> dict:
    return {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": stage.log_group_arn(f"/aws/lambda/{function_name}") + ":*"}


XRAY = {"Effect": "Allow", "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], "Resource": "*"}
