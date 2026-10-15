# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/infrastructure/bootstrap/state_backend.py (botocore Stubber, no network)."""
import json

import botocore.session
from botocore.stub import Stubber
import pytest

from module11.infrastructure.bootstrap import state_backend as sb

ACCOUNT = "111122223333"
REGION = "us-east-1"
BACKENDS = sb.backends(ACCOUNT, REGION)


def _client(service):
    session = botocore.session.Session()
    return session.create_client(service, region_name=REGION,  # nosec B106 - dummy credentials for an offline, stubbed client
                                 aws_access_key_id="testing", aws_secret_access_key="testing")


@pytest.fixture
def clients():
    sts, s3, kms = _client("sts"), _client("s3"), _client("kms")
    stubs = {name: Stubber(c) for name, c in (("sts", sts), ("s3", s3), ("kms", kms))}
    for stub in stubs.values():
        stub.activate()
    yield {"sts": sts, "s3": s3, "kms": kms}, stubs
    for stub in stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _key_arn(i):
    return f"arn:aws:kms:{REGION}:{ACCOUNT}:key/0000000{i}-0000-0000-0000-000000000000"


def _identity(stubs, account=ACCOUNT):
    stubs["sts"].add_response("get_caller_identity", {
        "Account": account, "Arn": f"arn:aws:iam::{account}:user/operator", "UserId": "AIDAEXAMPLE"})


def _stub_create(stubs, i, backend):
    kms, s3 = stubs["kms"], stubs["s3"]
    key_id = _key_arn(i).rsplit("/", 1)[1]
    kms.add_client_error("describe_key", service_error_code="NotFoundException", http_status_code=400,
                         expected_params={"KeyId": backend.alias})
    kms.add_response("create_key", {"KeyMetadata": {"KeyId": key_id, "Arn": _key_arn(i)}}, {
        "Description": f"Pulumi secrets provider for {backend.bucket}",
        "KeyUsage": "ENCRYPT_DECRYPT", "KeySpec": "SYMMETRIC_DEFAULT",
        "Tags": [{"TagKey": "Project", "TagValue": sb.PROJECT}, {"TagKey": "Stage", "TagValue": backend.stage}]})
    kms.add_response("create_alias", {}, {"AliasName": backend.alias, "TargetKeyId": key_id})
    kms.add_response("enable_key_rotation", {}, {"KeyId": key_id})

    s3.add_client_error("head_bucket", service_error_code="404", http_status_code=404,
                        expected_params={"Bucket": backend.bucket, "ExpectedBucketOwner": ACCOUNT})
    s3.add_response("create_bucket", {"Location": f"/{backend.bucket}"},
                    {"Bucket": backend.bucket, "ObjectOwnership": "BucketOwnerEnforced"})
    s3.add_response("put_bucket_tagging", {}, {"Bucket": backend.bucket, "Tagging": {"TagSet": [
        {"Key": "Project", "Value": sb.PROJECT}, {"Key": "Stage", "Value": backend.stage}]}})
    s3.add_response("put_public_access_block", {}, {
        "Bucket": backend.bucket, "PublicAccessBlockConfiguration": sb.PUBLIC_ACCESS_BLOCK})
    s3.add_response("put_bucket_versioning", {}, {
        "Bucket": backend.bucket, "VersioningConfiguration": {"Status": "Enabled"}})
    s3.add_response("put_bucket_encryption", {}, {
        "Bucket": backend.bucket, "ServerSideEncryptionConfiguration": {"Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": _key_arn(i)},
            "BucketKeyEnabled": True}]}})
    s3.add_response("put_bucket_policy", {}, {"Bucket": backend.bucket, "Policy": json.dumps(
        {"Version": "2012-10-17", "Statement": [sb._tls_statement(backend.bucket)]})})


def _stub_existing(stubs, i, backend, *, project=sb.PROJECT):
    kms, s3 = stubs["kms"], stubs["s3"]
    key_id = _key_arn(i).rsplit("/", 1)[1]
    kms.add_response("describe_key", {"KeyMetadata": {"KeyId": key_id, "Arn": _key_arn(i), "KeyState": "Enabled"}},
                     {"KeyId": backend.alias})
    kms.add_response("list_resource_tags", {"Tags": [
        {"TagKey": "Project", "TagValue": sb.PROJECT}, {"TagKey": "Stage", "TagValue": backend.stage}]},
        {"KeyId": key_id})
    kms.add_response("get_key_rotation_status", {"KeyRotationEnabled": True}, {"KeyId": key_id})

    s3.add_response("head_bucket", {}, {"Bucket": backend.bucket, "ExpectedBucketOwner": ACCOUNT})
    s3.add_response("get_bucket_tagging", {"TagSet": [
        {"Key": "Project", "Value": project}, {"Key": "Stage", "Value": backend.stage}]}, {"Bucket": backend.bucket})
    if project != sb.PROJECT:
        return
    s3.add_response("get_public_access_block", {"PublicAccessBlockConfiguration": sb.PUBLIC_ACCESS_BLOCK},
                    {"Bucket": backend.bucket})
    s3.add_response("get_bucket_versioning", {"Status": "Enabled"}, {"Bucket": backend.bucket})
    s3.add_response("get_bucket_encryption", {"ServerSideEncryptionConfiguration": {"Rules": [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": _key_arn(i)},
        "BucketKeyEnabled": True}]}}, {"Bucket": backend.bucket})
    s3.add_response("get_bucket_policy", {"Policy": json.dumps(
        {"Version": "2012-10-17", "Statement": [sb._tls_statement(backend.bucket)]})}, {"Bucket": backend.bucket})


def test_backend_names_come_from_identity():
    assert [b.bucket for b in BACKENDS] == [
        f"m11-pulumi-state-{ACCOUNT}-{REGION}", f"m11-pulumi-state-preview-{ACCOUNT}-{REGION}"]
    assert [b.alias for b in BACKENDS] == ["alias/m11-pulumi-secrets", "alias/m11-pulumi-secrets-preview"]


def test_create_path(clients):
    c, stubs = clients
    _identity(stubs)
    for i, backend in enumerate(BACKENDS):
        _stub_create(stubs, i, backend)
    actions = sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION, out=lambda _: None)
    assert len(actions) == 4
    assert all(a.startswith("create ") for a in actions)


def test_dry_run_on_empty_account_writes_nothing(clients):
    c, stubs = clients
    _identity(stubs)
    for backend in BACKENDS:
        stubs["kms"].add_client_error("describe_key", service_error_code="NotFoundException", http_status_code=400)
        stubs["s3"].add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    lines = []
    actions = sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION,
                     dry_run=True, out=lines.append)
    assert len(actions) == 4
    assert all(line.startswith("Would: ") for line in lines[:4])


def test_idempotent_rerun_changes_nothing(clients):
    c, stubs = clients
    _identity(stubs)
    for i, backend in enumerate(BACKENDS):
        _stub_existing(stubs, i, backend)
    lines = []
    actions = sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION, out=lines.append)
    assert actions == []
    assert lines[0].startswith("Nothing to create or change")


def test_wrong_account_refused_before_any_call(clients):
    c, stubs = clients
    _identity(stubs, account="444455556666")
    with pytest.raises(sb.BackendError, match="refusing to run"):
        sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION, out=lambda _: None)


def test_missing_expected_account_refused(clients):
    c, _ = clients
    with pytest.raises(sb.BackendError, match="M11_EXPECTED_ACCOUNT"):
        sb.run(c["sts"], c["s3"], c["kms"], expected_account=None, region=REGION, out=lambda _: None)


def test_foreign_bucket_refused(clients):
    c, stubs = clients
    _identity(stubs)
    _stub_existing(stubs, 0, BACKENDS[0], project="someone-else")
    with pytest.raises(sb.BackendError, match="refusing to adopt"):
        sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION, out=lambda _: None)


def test_bucket_owned_by_another_account_refused(clients):
    c, stubs = clients
    _identity(stubs)
    key_id = _key_arn(0).rsplit("/", 1)[1]
    stubs["kms"].add_response("describe_key", {"KeyMetadata": {"KeyId": key_id, "Arn": _key_arn(0),
                                                                "KeyState": "Enabled"}})
    stubs["kms"].add_response("list_resource_tags", {"Tags": [{"TagKey": "Project", "TagValue": sb.PROJECT}]})
    stubs["kms"].add_response("get_key_rotation_status", {"KeyRotationEnabled": True})
    stubs["s3"].add_client_error("head_bucket", service_error_code="403", http_status_code=403)
    with pytest.raises(sb.BackendError, match="not accessible"):
        sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION, out=lambda _: None)


def test_foreign_key_refused(clients):
    c, stubs = clients
    _identity(stubs)
    key_id = _key_arn(0).rsplit("/", 1)[1]
    stubs["kms"].add_response("describe_key", {"KeyMetadata": {"KeyId": key_id, "Arn": _key_arn(0),
                                                                "KeyState": "Enabled"}})
    stubs["kms"].add_response("list_resource_tags", {"Tags": []})
    with pytest.raises(sb.BackendError, match="refusing to adopt"):
        sb.run(c["sts"], c["s3"], c["kms"], expected_account=ACCOUNT, region=REGION, out=lambda _: None)
