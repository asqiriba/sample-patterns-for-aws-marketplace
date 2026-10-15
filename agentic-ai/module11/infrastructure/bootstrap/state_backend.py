# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create or verify the two self-managed Pulumi state backends for Module 11.

A Pulumi backend cannot hold its own state, so this script (boto3 only,
idempotent) creates both backends before any Pulumi program runs:

- main:    s3://m11-pulumi-state-<account>-<region>          + alias/m11-pulumi-secrets
- preview: s3://m11-pulumi-state-preview-<account>-<region>  + alias/m11-pulumi-secrets-preview

Each bucket is versioned, encrypted with SSE-KMS under its own key (rotation
on), blocks all public access, and denies any request that is not TLS. Names
come from the caller identity, so nothing account-specific is hard-coded.

Safety: the script refuses to run unless the caller's account equals
M11_EXPECTED_ACCOUNT (or --expected-account), and it adopts an existing
bucket or key only when it is tagged Project=m11-devops-companion.

Cost: two customer-managed KMS keys (about $1/month each plus requests) and
a few MB of versioned state in S3 (cents per month).

Usage (from agentic-ai/):
    python -m module11.infrastructure.bootstrap.state_backend --dry-run
    python -m module11.infrastructure.bootstrap.state_backend
"""
import argparse
from dataclasses import dataclass
import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

PROJECT = "m11-devops-companion"
TLS_SID = "DenyInsecureTransport"


class BackendError(Exception):
    """A resource exists but is not ours, or the environment is wrong."""


@dataclass(frozen=True)
class Backend:
    stage: str
    bucket: str
    alias: str

    @property
    def tags(self):
        return {"Project": PROJECT, "Stage": self.stage}


def backends(account, region):
    """The main backend (dev, staging, prod, platform, partners, Module 10) and the mr-* preview backend."""
    return [
        Backend("shared", f"m11-pulumi-state-{account}-{region}", "alias/m11-pulumi-secrets"),
        Backend("preview", f"m11-pulumi-state-preview-{account}-{region}", "alias/m11-pulumi-secrets-preview"),
    ]


def _code(error):
    return error.response.get("Error", {}).get("Code", "")


def _tls_statement(bucket):
    return {
        "Sid": TLS_SID,
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
    }


def _encryption(key_arn):
    return {"Rules": [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": key_arn},
        "BucketKeyEnabled": True,
    }]}


PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True, "IgnorePublicAcls": True,
    "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
}


def ensure_key(kms, backend, *, dry_run, actions):
    """Return the key ARN behind the alias (None in a dry run that would create it)."""
    try:
        meta = kms.describe_key(KeyId=backend.alias)["KeyMetadata"]
    except ClientError as error:
        if _code(error) != "NotFoundException":
            raise
        actions.append(f"create KMS key {backend.alias} (symmetric, rotation on)")
        if dry_run:
            return None
        meta = kms.create_key(
            Description=f"Pulumi secrets provider for {backend.bucket}",
            KeyUsage="ENCRYPT_DECRYPT", KeySpec="SYMMETRIC_DEFAULT",
            Tags=[{"TagKey": k, "TagValue": v} for k, v in backend.tags.items()],
        )["KeyMetadata"]
        kms.create_alias(AliasName=backend.alias, TargetKeyId=meta["KeyId"])
        kms.enable_key_rotation(KeyId=meta["KeyId"])
        return meta["Arn"]

    tags = {t["TagKey"]: t["TagValue"] for t in kms.list_resource_tags(KeyId=meta["KeyId"])["Tags"]}
    if tags.get("Project") != PROJECT:
        raise BackendError(f"{backend.alias} points at a key not tagged Project={PROJECT}; refusing to adopt it")
    if meta.get("KeyState") != "Enabled":
        raise BackendError(f"{backend.alias} key is {meta.get('KeyState')}; enable it or delete the alias first")
    if not kms.get_key_rotation_status(KeyId=meta["KeyId"])["KeyRotationEnabled"]:
        actions.append(f"enable rotation on {backend.alias}")
        if not dry_run:
            kms.enable_key_rotation(KeyId=meta["KeyId"])
    return meta["Arn"]


def _configure_new_bucket(s3, backend, key_arn):
    s3.put_bucket_tagging(Bucket=backend.bucket, Tagging={
        "TagSet": [{"Key": k, "Value": v} for k, v in backend.tags.items()]})
    s3.put_public_access_block(Bucket=backend.bucket, PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK)
    s3.put_bucket_versioning(Bucket=backend.bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_encryption(Bucket=backend.bucket, ServerSideEncryptionConfiguration=_encryption(key_arn))
    s3.put_bucket_policy(Bucket=backend.bucket, Policy=json.dumps(
        {"Version": "2012-10-17", "Statement": [_tls_statement(backend.bucket)]}))


def ensure_bucket(s3, backend, account, region, key_arn, *, dry_run, actions):
    bucket = backend.bucket
    try:
        s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=account)
    except ClientError as error:
        if _code(error) not in ("404", "NoSuchBucket"):
            raise BackendError(f"bucket {bucket} exists but is not accessible in this account ({_code(error)})") from error
        actions.append(f"create bucket {bucket} (versioned, SSE-KMS {backend.alias}, public access blocked, TLS only)")
        if dry_run:
            return
        create = {"Bucket": bucket, "ObjectOwnership": "BucketOwnerEnforced"}
        if region != "us-east-1":
            create["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**create)
        _configure_new_bucket(s3, backend, key_arn)
        return

    # Existing bucket: adopt it only if this project tagged it.
    try:
        tags = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
    except ClientError as error:
        if _code(error) != "NoSuchTagSet":
            raise
        tags = {}
    if tags.get("Project") != PROJECT:
        raise BackendError(f"bucket {bucket} exists without tag Project={PROJECT}; refusing to adopt it")
    if any(tags.get(k) != v for k, v in backend.tags.items()):
        actions.append(f"tag bucket {bucket} {backend.tags}")
        if not dry_run:
            s3.put_bucket_tagging(Bucket=bucket, Tagging={
                "TagSet": [{"Key": k, "Value": v} for k, v in {**tags, **backend.tags}.items()]})

    try:
        pab = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    except ClientError as error:
        if _code(error) != "NoSuchPublicAccessBlockConfiguration":
            raise
        pab = {}
    if any(not pab.get(k) for k in PUBLIC_ACCESS_BLOCK):
        actions.append(f"block public access on {bucket}")
        if not dry_run:
            s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration=PUBLIC_ACCESS_BLOCK)

    if s3.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        actions.append(f"enable versioning on {bucket}")
        if not dry_run:
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

    try:
        rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
    except ClientError as error:
        if _code(error) != "ServerSideEncryptionConfigurationNotFoundError":
            raise
        rules = []
    default = rules[0].get("ApplyServerSideEncryptionByDefault", {}) if rules else {}
    if key_arn is None or default.get("SSEAlgorithm") != "aws:kms" or default.get("KMSMasterKeyID") != key_arn \
            or not rules[0].get("BucketKeyEnabled"):
        actions.append(f"set SSE-KMS ({backend.alias}) on {bucket}")
        if not dry_run:
            s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration=_encryption(key_arn))

    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    except ClientError as error:
        if _code(error) != "NoSuchBucketPolicy":
            raise
        policy = {"Version": "2012-10-17", "Statement": []}
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    wanted = _tls_statement(bucket)
    if wanted not in statements:
        actions.append(f"add {TLS_SID} to the bucket policy of {bucket}")
        if not dry_run:
            # Keep any other statement; replace only ours.
            policy["Statement"] = [s for s in statements if s.get("Sid") != TLS_SID] + [wanted]
            s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))


def run(sts, s3, kms, *, expected_account, region, dry_run=False, out=print):
    """Ensure both backends; return the list of actions taken (or planned in a dry run)."""
    if not expected_account:
        raise BackendError("set M11_EXPECTED_ACCOUNT (or --expected-account) to the target account ID")
    if not region:
        raise BackendError("no AWS region configured; set AWS_REGION")
    account = sts.get_caller_identity()["Account"]
    if account != expected_account:
        raise BackendError(f"caller account {account} is not M11_EXPECTED_ACCOUNT {expected_account}; refusing to run")
    actions = []
    for backend in backends(account, region):
        key_arn = ensure_key(kms, backend, dry_run=dry_run, actions=actions)
        ensure_bucket(s3, backend, account, region, key_arn, dry_run=dry_run, actions=actions)
    verb = "Would" if dry_run else "Did"
    for action in actions:
        out(f"{verb}: {action}")
    if not actions:
        out("Nothing to create or change: both state backends are in place.")
    for backend in backends(account, region):
        out(f"{backend.stage:8} s3://{backend.bucket}  awskms://{backend.alias}?region={region}")
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    parser.add_argument("--expected-account", default=os.environ.get("M11_EXPECTED_ACCOUNT"),
                        help="refuse to run in any other account (default: $M11_EXPECTED_ACCOUNT)")
    args = parser.parse_args(argv)
    session = boto3.Session()
    try:
        run(session.client("sts"), session.client("s3"), session.client("kms"),
            expected_account=args.expected_account, region=session.region_name, dry_run=args.dry_run)
    except BackendError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
