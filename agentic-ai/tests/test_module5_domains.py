# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_module5_domains.py
==============================
Offline smoke tests for the Module 5 Domain Adaptation Engine.

Covers DomainConfig validation, the four-layer PromptBuilder, the three
shipped domain configs against the shared tool registry, and
DomainAdapter.adapt with a ChatBedrock built offline (never invoked). In mock
mode no Bedrock Guardrails call is made. No AWS credentials needed.

Run:
    AGENT_MOCK_DOMAIN=true pytest tests/test_module5_domains.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("AGENT_MOCK_DOMAIN", "true")

import pytest

from module5.engine.domain_adapter import (
    DomainAdapter,
    DomainAgent,
    DomainConfig,
    GuardrailPolicy,
    PromptBuilder,
    PromptLayers,
)


def _layers() -> PromptLayers:
    return PromptLayers(
        role="ROLE-TEXT",
        knowledge_context="KNOWLEDGE-TEXT",
        constraints="CONSTRAINTS-TEXT",
        communication="COMMUNICATION-TEXT",
    )


class TestDomainConfigValidation:
    @pytest.mark.parametrize("bad_name", ["", "Customer", "customer-1", "with space"])
    def test_rejects_bad_name(self, bad_name):
        with pytest.raises(ValueError, match="name"):
            DomainConfig(
                name=bad_name,
                display_name="x",
                prompt_layers=_layers(),
                tool_names=["algolia_semantic_search"],
                guardrail_policy=GuardrailPolicy(),
            )

    def test_rejects_empty_tool_names(self):
        with pytest.raises(ValueError, match="tool_names"):
            DomainConfig(
                name="valid_name",
                display_name="x",
                prompt_layers=_layers(),
                tool_names=[],
                guardrail_policy=GuardrailPolicy(),
            )

    def test_rejects_invalid_pii_handling(self):
        with pytest.raises(ValueError, match="pii_handling"):
            DomainConfig(
                name="valid_name",
                display_name="x",
                prompt_layers=_layers(),
                tool_names=["algolia_semantic_search"],
                guardrail_policy=GuardrailPolicy(pii_handling="REDACT"),
            )


class TestPromptBuilder:
    def test_compose_contains_four_layers_in_order(self):
        prompt = PromptBuilder().compose(_layers())

        markers = [
            "## Role", "ROLE-TEXT",
            "## Knowledge Context", "KNOWLEDGE-TEXT",
            "## Constraints", "CONSTRAINTS-TEXT",
            "## Communication", "COMMUNICATION-TEXT",
        ]
        positions = [prompt.index(m) for m in markers]
        assert positions == sorted(positions)


class TestShippedDomains:
    def test_registry_has_sixteen_tools(self):
        from module5.tools import ALL_TOOLS

        assert len(ALL_TOOLS) == 16
        assert all(name == tool.name for name, tool in ALL_TOOLS.items())

    @pytest.mark.parametrize("domain_attr", ["CUSTOMER_DOMAIN", "GEOSPATIAL_DOMAIN", "VOICE_DOMAIN"])
    def test_domain_tools_exist_in_registry(self, domain_attr):
        import module5.domains as domains
        from module5.tools import ALL_TOOLS

        config = getattr(domains, domain_attr)
        assert config.tool_names
        assert set(config.tool_names) <= set(ALL_TOOLS)


class TestDomainAdapter:
    def test_adapt_builds_domain_agent_without_guardrail_call(self, monkeypatch):
        import boto3
        from langchain_aws import ChatBedrock
        from module5.config.models import get_chat_bedrock_model
        from module5.domains import CUSTOMER_DOMAIN
        from module5.tools import ALL_TOOLS

        # Construct the model before blocking boto3: ChatBedrock creates its
        # (unused) runtime client at construction time.
        model = get_chat_bedrock_model(region="us-east-1")
        assert isinstance(model, ChatBedrock)

        def _no_aws(*_args, **_kwargs):
            raise AssertionError("mock mode must not create a Bedrock Guardrails client")

        monkeypatch.setattr(boto3, "client", _no_aws)

        result = DomainAdapter(ALL_TOOLS).adapt(base_model=model, config=CUSTOMER_DOMAIN)

        assert isinstance(result, DomainAgent)
        assert result.config is CUSTOMER_DOMAIN
        assert result.system_prompt == PromptBuilder().compose(CUSTOMER_DOMAIN.prompt_layers)
        assert result.guardrail_id == "mock-guardrail-customer_engagement"
        assert set(result.agent.nodes["tools"].bound.tools_by_name) == set(CUSTOMER_DOMAIN.tool_names)
