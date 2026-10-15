# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guardrail projection. Pure.

`guardrail.yaml` uses Module 5's GuardrailPolicy vocabulary. `to_request()` produces the
CreateGuardrail / aws.bedrock.Guardrail fields. `project_request()` and `project_response()`
(over a GetGuardrail response) yield the same canonical dict, so the manifest's
`guardrail.definitionSha256` and the drift checker's hash of the live version agree. Response
fields at their defaults (enabled flags, TEXT modalities, BLOCK actions), status and timestamps
are dropped; a non-default value stays in the projection, so it shows up as drift.
"""
from __future__ import annotations

from typing import Any, Mapping

from .hashing import json_hash

NO_OUTPUT_STRENGTH = {"PROMPT_ATTACK"}     # Bedrock requires outputStrength NONE for prompt attacks
_FILTER_DEFAULTS = {"inputEnabled": True, "outputEnabled": True, "inputAction": "BLOCK", "outputAction": "BLOCK",
                    "inputModalities": ["TEXT"], "outputModalities": ["TEXT"]}
_PII_DEFAULTS = {"inputEnabled": True, "outputEnabled": True}
_TOPIC_DEFAULTS = {"inputEnabled": True, "outputEnabled": True, "inputAction": "BLOCK", "outputAction": "BLOCK"}
_WORD_DEFAULTS = {"inputEnabled": True, "outputEnabled": True, "inputAction": "BLOCK", "outputAction": "BLOCK"}


def to_request(doc: Mapping[str, Any]) -> dict:
    """guardrail.yaml -> CreateGuardrail policy fields (name, description and KMS are the program's)."""
    action = doc.get("pii_handling", "ANONYMIZE")
    req: dict[str, Any] = {
        "blockedInputMessaging": doc["blocked_input_messaging"],
        "blockedOutputsMessaging": doc["blocked_outputs_messaging"],
    }
    filters = [{"type": cat, "inputStrength": strength,
                "outputStrength": "NONE" if cat in NO_OUTPUT_STRENGTH else strength}
               for cat, strength in sorted((doc.get("content_filters") or {}).items())]
    if filters:
        req["contentPolicyConfig"] = {"filtersConfig": filters}
    pii = [{"type": entity, "action": action} for entity in doc.get("pii_entities") or []]
    if pii:
        req["sensitiveInformationPolicyConfig"] = {"piiEntitiesConfig": pii}
    topics = [{"name": t["name"], "definition": t["definition"], "examples": list(t.get("examples") or []),
               "type": "DENY"} for t in doc.get("denied_topics") or []]
    if topics:
        req["topicPolicyConfig"] = {"topicsConfig": topics}
    words = [{"text": w} for w in doc.get("word_filters") or []]
    if words:
        req["wordPolicyConfig"] = {"wordsConfig": words}
    return req


def _extras(entry: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict:
    return {k: entry[k] for k in sorted(defaults) if k in entry and entry[k] != defaults[k]}


def _canonical(blocked_in: str, blocked_out: str, content, pii, topics, words) -> dict:
    return {"blockedInputMessaging": blocked_in, "blockedOutputsMessaging": blocked_out,
            "content": sorted(content), "pii": sorted(pii), "topics": sorted(topics), "words": sorted(words)}


def project_request(req: Mapping[str, Any]) -> dict:
    content = [[f["type"], f["inputStrength"], f["outputStrength"]]
               for f in (req.get("contentPolicyConfig") or {}).get("filtersConfig") or []]
    pii = [[e["type"], e["action"]] for e in (req.get("sensitiveInformationPolicyConfig") or {}).get("piiEntitiesConfig") or []]
    topics = [[t["name"], t["definition"], t.get("type", "DENY"), sorted(t.get("examples") or [])]
              for t in (req.get("topicPolicyConfig") or {}).get("topicsConfig") or []]
    words = [w["text"] for w in (req.get("wordPolicyConfig") or {}).get("wordsConfig") or []]
    return _canonical(req["blockedInputMessaging"], req["blockedOutputsMessaging"], content, pii, topics, words)


def project_response(resp: Mapping[str, Any]) -> dict:
    """GetGuardrail(id, version) response -> the canonical dict."""
    content = []
    for f in (resp.get("contentPolicy") or {}).get("filters") or []:
        row = [f["type"], f["inputStrength"], f["outputStrength"]]
        extra = _extras(f, _FILTER_DEFAULTS)
        content.append(row + [extra] if extra else row)
    pii = []
    for e in (resp.get("sensitiveInformationPolicy") or {}).get("piiEntities") or []:
        row = [e["type"], e["action"]]
        extra = _extras(e, _PII_DEFAULTS)
        pii.append(row + [extra] if extra else row)
    topics = []
    for t in (resp.get("topicPolicy") or {}).get("topics") or []:
        row = [t["name"], t["definition"], t.get("type", "DENY"), sorted(t.get("examples") or [])]
        extra = _extras(t, _TOPIC_DEFAULTS)
        topics.append(row + [extra] if extra else row)
    words = []
    for w in (resp.get("wordPolicy") or {}).get("words") or []:
        extra = _extras(w, _WORD_DEFAULTS)
        words.append(w["text"] if not extra else f"{w['text']} {sorted(extra.items())}")
    return _canonical(resp["blockedInputMessaging"], resp["blockedOutputsMessaging"], content, pii, topics, words)


def definition_sha256(doc: Mapping[str, Any]) -> str:
    """The manifest's guardrail.definitionSha256."""
    return json_hash(project_request(to_request(doc)))


def response_sha256(resp: Mapping[str, Any]) -> str:
    """The drift checker's hash of a live guardrail version."""
    return json_hash(project_response(resp))
