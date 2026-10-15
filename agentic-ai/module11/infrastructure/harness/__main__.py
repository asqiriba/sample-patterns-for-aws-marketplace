# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""m11-harness: one stage of the DevOps Companion harness.

Stacks `dev`, `staging`, `prod` live in the main S3 backend; `mr-<iid>` stacks in the preview
backend. A local `pulumi preview` works anywhere; a staging or prod `up` goes through
`python -m module11 release deploy --stage <s>`, which assumes the stage deployer (apply guard, M7).
Release inputs come from M11_RELEASE_INPUTS (set only by `release deploy` / `release prune`), else
from SSM `/applied` (sha-verified), else ReleaseInputsMissing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
import pulumi  # noqa: E402
import pulumi_aws as aws  # noqa: E402

from module11.aws import params  # noqa: E402
from module11.infrastructure.harness import program  # noqa: E402

_session = boto3.session.Session()


def _read_s3(uri: str) -> bytes:
    bucket, key = uri[len("s3://"):].split("/", 1)
    return _session.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()


def _read_file(path: str) -> bytes:
    return Path(path).read_bytes()


def _get_applied(stage: str):
    return params.stage_get(_session.client("ssm"), stage, "applied")


def _shared_outputs():
    return params.shared_outputs(_session.client("ssm"))


caller = aws.get_caller_identity()
program.run(program.Sources(read_s3=_read_s3, read_file=_read_file, get_applied=_get_applied,
                            shared_outputs=_shared_outputs),
            stack=pulumi.get_stack(), account=caller.account_id, region=aws.get_region().region,
            caller_arn=caller.arn, dry_run=pulumi.runtime.is_dry_run(), env=dict(os.environ))
