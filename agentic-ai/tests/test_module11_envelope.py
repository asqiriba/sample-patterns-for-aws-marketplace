# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Runtime envelope validation (8.1 test_module11_envelope)."""
import copy

import pytest

from module11.runtime import envelope as E

GOOD = {
    "identity": {"team": "platform", "principal": "user-1"},
    "request": {"change_summary": "  checkout 502s since the deploy  ", "request_id": "req-1"},
    "context": {"variant": "stable", "conversationId": "conv_ABC-123",
                "manifestVersion": "2026-10-20T14:02:11Z/abc1234",
                "behaviorDigest": "sha256:" + "a" * 64, "release": "harness-v8"},
}


def env(**changes):
    doc = copy.deepcopy(GOOD)
    for path, value in changes.items():
        section, _, key = path.partition("__")
        if value is ...:
            doc[section].pop(key, None)
        else:
            doc[section][key] = value
    return doc


def test_valid_envelope_trims_and_keeps_identity():
    inv = E.parse(GOOD)
    assert inv.change_summary == "checkout 502s since the deploy"
    assert inv.identity() == {"manifestVersion": "2026-10-20T14:02:11Z/abc1234",
                              "behaviorDigest": "sha256:" + "a" * 64, "release": "harness-v8"}
    assert (inv.team, inv.principal, inv.conversation_id, inv.shadow) == ("platform", "user-1", "conv_ABC-123", False)


def test_conversation_defaults_to_request_id():
    assert E.parse(env(context__conversationId=...)).conversation_id == "req-1"


@pytest.mark.parametrize("doc", [
    {**GOOD, "extra": {}},
    env(identity__role="admin"),
    env(request__shadow=True),
    env(context__scenario="x"),
    env(context__capture_session="s"),
])
def test_unknown_keys_are_rejected(doc):
    with pytest.raises(E.EnvelopeError):
        E.parse(doc)


@pytest.mark.parametrize("variant", sorted(E.VARIANTS - E.SHADOW_VARIANTS))
def test_shadow_flag_on_a_user_variant_is_rejected(variant):
    with pytest.raises(E.EnvelopeError, match="shadow"):
        E.parse(env(context__variant=variant, context__shadow=True))


@pytest.mark.parametrize("variant", sorted(E.SHADOW_VARIANTS))
def test_shadow_flag_accepted_for_shadow_and_eval(variant):
    assert E.parse(env(context__variant=variant, context__shadow=True)).shadow is True


@pytest.mark.parametrize("key,value", [
    ("manifestVersion", ...), ("manifestVersion", "2026-10-20/abc1234"), ("manifestVersion", "2026-10-20T14:02:11Z/ABC1234"),
    ("behaviorDigest", ...), ("behaviorDigest", "sha256:" + "a" * 63), ("behaviorDigest", "a" * 64),
    ("release", ...), ("release", "Harness-V8"), ("release", "x" * 41), ("release", "harness/v8"),
])
def test_release_identity_is_required_and_patterned(key, value):
    with pytest.raises(E.EnvelopeError):
        E.parse(env(**{f"context__{key}": value}))


@pytest.mark.parametrize("path,value", [
    ("identity__team", "plat form"), ("identity__principal", "x" * 129), ("request__request_id", "bad id"),
    ("context__conversationId", "short"), ("context__conversationId", "x" * 65), ("context__variant", "prod"),
    ("context__shadow", "true"), ("context__rolloutId", "r/1"), ("context__traceparent", "00-xyz"),
    ("context__scenarioId", "Bad_ID"), ("request__change_summary", "   "), ("request__change_summary", "x" * 4001),
])
def test_id_patterns(path, value):
    with pytest.raises(E.EnvelopeError):
        E.parse(env(**{path: value}))


def test_missing_context_or_request_is_rejected():
    with pytest.raises(E.EnvelopeError):
        E.parse({"identity": GOOD["identity"], "request": GOOD["request"]})
    with pytest.raises(E.EnvelopeError):
        E.parse({"identity": GOOD["identity"], "context": GOOD["context"]})
    with pytest.raises(E.EnvelopeError):
        E.parse([GOOD])


def test_optional_context_fields():
    inv = E.parse(env(context__traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
                      context__rolloutId="r-0123456789abcdef", context__scenarioId="incident-triage-014"))
    assert (inv.rollout_id, inv.scenario_id) == ("r-0123456789abcdef", "incident-triage-014")
