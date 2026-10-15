# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guardrail and its retained versions (declared here, taught in Modules 5/7).

The definition comes from `release/guardrail.py: to_request()` over the sealed `guardrail.yaml`. A
changed definition changes the version's description (`m11 <definitionSha256 hex[:16]>`), which
forces a replacement and so a new version N+1; `skip_destroy=True` keeps version N, which stable
still pins (H2). Teardown deletes the guardrail, which deletes every version.
"""
from __future__ import annotations

import pulumi
import pulumi_aws as aws

from module11.release import guardrail as projection
from module11.release.hashing import hex_of

from .base import Stage


def _pulumi_args(request: dict) -> dict:
    """CreateGuardrail field names (camelCase) -> aws.bedrock.Guardrail arguments."""
    out = {"blocked_input_messaging": request["blockedInputMessaging"],
           "blocked_outputs_messaging": request["blockedOutputsMessaging"]}
    if "contentPolicyConfig" in request:
        out["content_policy_config"] = {"filters_configs": [
            {"type": f["type"], "input_strength": f["inputStrength"], "output_strength": f["outputStrength"]}
            for f in request["contentPolicyConfig"]["filtersConfig"]]}
    if "sensitiveInformationPolicyConfig" in request:
        out["sensitive_information_policy_config"] = {"pii_entities_configs": [
            {"type": e["type"], "action": e["action"]}
            for e in request["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]]}
    if "topicPolicyConfig" in request:
        out["topic_policy_config"] = {"topics_configs": [
            {"name": t["name"], "definition": t["definition"], "examples": t["examples"], "type": t["type"]}
            for t in request["topicPolicyConfig"]["topicsConfig"]]}
    if "wordPolicyConfig" in request:
        out["word_policy_config"] = {"words_configs": [{"text": w["text"]} for w in request["wordPolicyConfig"]["wordsConfig"]]}
    return out


def declare(stage: Stage, definition: dict) -> tuple[aws.bedrock.Guardrail, aws.bedrock.GuardrailVersion, str]:
    request = projection.to_request(definition)
    digest = projection.definition_sha256(definition)
    guard = aws.bedrock.Guardrail("guardrail", name=f"m11-{stage.name}-companion",
                                  description="DevOps Companion guardrail (Module 11, declared in m11-harness)",
                                  tags=stage.tags, **_pulumi_args(request))
    version = aws.bedrock.GuardrailVersion(
        "guardrail-version", guardrail_arn=guard.guardrail_arn, description=f"m11 {hex_of(digest)[:16]}",
        skip_destroy=True, opts=pulumi.ResourceOptions(depends_on=[guard], replace_on_changes=["description"]))
    return guard, version, digest
