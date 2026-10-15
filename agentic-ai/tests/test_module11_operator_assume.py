# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Operator sessions with botocore Stubber: an IAM-user caller always passes
SourceIdentity; an assumed-role caller omits it first and retries exactly once on AccessDenied; the
audit record takes the source identity from the AssumeRole response."""
from datetime import datetime, timedelta, timezone

import botocore.session
import pytest
from botocore.stub import Stubber

from module11.aws import operator

ROLE = "arn:aws:iam::111122223333:role/m11-dev-deployer"
CREDS = {"AccessKeyId": "test-access-key-0000", "SecretAccessKey": "s" * 40, "SessionToken": "t" * 120,
         "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)}


def sts():
    client = botocore.session.get_session().create_client("sts", region_name="us-east-1",  # nosec B106 - dummy credentials for an offline, stubbed client
                                                          aws_access_key_id="x", aws_secret_access_key="y")
    return client, Stubber(client)


def _assumed(identity):
    return {"Credentials": CREDS, "AssumedRoleUser": {"AssumedRoleId": "AROAEXAMPLE:m11-operator-x",
                                                      "Arn": f"arn:aws:sts::111122223333:assumed-role/m11-dev-deployer/x"},
            **({"SourceIdentity": identity} if identity else {})}


def test_iam_user_always_passes_source_identity():
    client, stub = sts()
    stub.add_response("get_caller_identity", {"Arn": "arn:aws:iam::111122223333:user/alice", "Account": "111122223333",
                                              "UserId": "AIDA"})
    stub.add_response("assume_role", _assumed("alice"), {"RoleArn": ROLE, "RoleSessionName": "m11-operator-alice",
                                                         "DurationSeconds": 3600, "SourceIdentity": "alice"})
    records = []
    with stub:
        session = operator.assume(ROLE, sts=client, record=records.append, stage="dev", purpose="test")
    stub.assert_no_pending_responses()
    assert session.session_name == "m11-operator-alice" and session.source_identity == "alice"
    assert records[0]["sourceIdentity"] == "alice" and records[0]["kind"] == "operator-session"
    assert set(session.env()) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}


def test_assumed_role_caller_omits_then_retries_once():
    client, stub = sts()
    caller = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_Admin/bob@example.com"
    stub.add_response("get_caller_identity", {"Arn": caller, "Account": "111122223333", "UserId": "AROA:bob"})
    stub.add_client_error("assume_role", "AccessDenied", "no source identity",
                          expected_params={"RoleArn": ROLE, "RoleSessionName": "m11-operator-bob@example.com",
                                           "DurationSeconds": 3600})
    stub.add_response("assume_role", _assumed("bob@example.com"),
                      {"RoleArn": ROLE, "RoleSessionName": "m11-operator-bob@example.com", "DurationSeconds": 3600,
                       "SourceIdentity": "bob@example.com"})
    with stub:
        session = operator.assume(ROLE, sts=client)
    stub.assert_no_pending_responses()
    assert session.source_identity == "bob@example.com"


def test_propagated_source_identity_is_recorded_from_the_response():
    client, stub = sts()
    stub.add_response("get_caller_identity", {"Arn": "arn:aws:sts::111122223333:assumed-role/ci/job-1",
                                              "Account": "111122223333", "UserId": "AROA:job"})
    stub.add_response("assume_role", _assumed("carol"), {"RoleArn": ROLE, "RoleSessionName": "m11-operator-job-1",
                                                         "DurationSeconds": 3600})
    records = []
    with stub:
        session = operator.assume(ROLE, sts=client, record=records.append)
    assert session.source_identity == "carol" and records[0]["sourceIdentity"] == "carol"


def test_second_access_denied_is_raised():
    client, stub = sts()
    stub.add_response("get_caller_identity", {"Arn": "arn:aws:sts::111122223333:assumed-role/r/s",
                                              "Account": "111122223333", "UserId": "AROA:s"})
    stub.add_client_error("assume_role", "AccessDenied", "denied")
    stub.add_client_error("assume_role", "AccessDenied", "denied again")
    with stub, pytest.raises(operator.AssumeDenied):
        operator.assume(ROLE, sts=client)
    stub.assert_no_pending_responses()


def test_iam_user_denied_once_is_not_retried():
    client, stub = sts()
    stub.add_response("get_caller_identity", {"Arn": "arn:aws:iam::111122223333:user/alice", "Account": "111122223333",
                                              "UserId": "AIDA"})
    stub.add_client_error("assume_role", "AccessDenied", "denied")
    with stub, pytest.raises(operator.AssumeDenied):
        operator.assume(ROLE, sts=client)
    stub.assert_no_pending_responses()


def test_session_name_is_sanitized_and_bounded():
    name = operator.session_name("arn:aws:iam::111122223333:user/" + "a" * 80 + "!")
    assert name.startswith("m11-operator-") and len(name) <= 64 and "!" not in name


def test_an_inline_session_policy_is_passed_to_assume_role():
    client, stub = sts()
    policy = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"bedrock-agentcore:Evaluate","Resource":"*"}]}'
    stub.add_response("get_caller_identity", {"Arn": "arn:aws:iam::111122223333:user/alice", "Account": "111122223333",
                                              "UserId": "AIDA"})
    stub.add_response("assume_role", _assumed("alice"), {"RoleArn": ROLE, "RoleSessionName": "m11-operator-alice",
                                                         "DurationSeconds": 3600, "SourceIdentity": "alice",
                                                         "Policy": policy})
    with stub:
        operator.assume(ROLE, sts=client, policy=policy)
    stub.assert_no_pending_responses()
