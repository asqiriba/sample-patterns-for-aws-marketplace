# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guardrail projection. The projection cases use the frozen harness_min fixture (it keeps a
denied topic) with its synthetic GetGuardrail response; get_guardrail_live.json is the redacted response of the
guardrail version deployed for the real harness definition."""
import copy
import json
from pathlib import Path

from module11.release import guardrail as G
from module11.release import stage as cfg

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/module11/harness_min"
DOC = cfg.guardrail(FIXTURE)
RESPONSE = json.loads((ROOT / "tests/fixtures/module11/get_guardrail.json").read_text(encoding="utf-8"))
LIVE = json.loads((ROOT / "tests/fixtures/module11/get_guardrail_live.json").read_text(encoding="utf-8"))


def test_request_and_response_project_to_the_same_dict():
    assert G.project_request(G.to_request(DOC)) == G.project_response(RESPONSE)
    assert G.definition_sha256(DOC) == G.response_sha256(RESPONSE)


def test_request_fields():
    req = G.to_request(DOC)
    filters = {f["type"]: f for f in req["contentPolicyConfig"]["filtersConfig"]}
    assert filters["PROMPT_ATTACK"] == {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}
    assert {e["action"] for e in req["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]} == {"ANONYMIZE"}
    assert req["topicPolicyConfig"]["topicsConfig"][0]["type"] == "DENY"
    assert "wordPolicyConfig" not in req      # word_filters is empty


def test_order_does_not_change_the_hash_but_a_strength_does():
    shuffled = copy.deepcopy(DOC)
    shuffled["pii_entities"] = list(reversed(shuffled["pii_entities"]))
    shuffled["content_filters"] = dict(reversed(list(shuffled["content_filters"].items())))
    assert G.definition_sha256(shuffled) == G.definition_sha256(DOC)
    stronger = copy.deepcopy(DOC)
    stronger["content_filters"]["INSULTS"] = "HIGH"
    assert G.definition_sha256(stronger) != G.definition_sha256(DOC)


def test_non_default_response_flags_are_drift():
    edited = copy.deepcopy(RESPONSE)
    edited["contentPolicy"]["filters"][0]["outputEnabled"] = False
    assert G.response_sha256(edited) != G.definition_sha256(DOC)
    status_only = {**RESPONSE, "status": "UPDATING", "updatedAt": "2026-10-07T00:00:00Z", "version": "4"}
    assert G.response_sha256(status_only) == G.definition_sha256(DOC)


def test_manifest_records_the_definition_hash():
    from module11.cli import git_info
    from module11.release import manifest as M
    assert M.build_declared(ROOT, git_info(ROOT), with_handlers=False)["guardrail"]["definitionSha256"] == \
        G.definition_sha256(cfg.guardrail(ROOT))


def test_live_capture_matches_the_harness_definition():
    """The deployed version's GetGuardrail response projects to the declared definition hash (no drift)."""
    assert G.response_sha256(LIVE) == G.definition_sha256(cfg.guardrail(ROOT))
    assert LIVE["description"].startswith("m11 ") and "topicPolicy" not in LIVE
