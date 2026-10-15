# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_module7_agent.py
============================
Tests for the memory-augmented agent.
All tests run with AGENT_MOCK_MEMORY=true via conftest autouse fixture.

Offline by default: agent construction and config tests need no AWS access.
The three tests that invoke the agent make live Bedrock calls and are opt-in:

    RUN_INTEGRATION_TESTS=true AGENT_MOCK_MEMORY=true pytest tests/test_module7_agent.py -v
"""
import os

import pytest

from module7 import MemoryDomainConfig, create_memory_agent

live_bedrock = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS", "").lower() != "true",
    reason="live Bedrock: set RUN_INTEGRATION_TESTS=true and AWS credentials",
)


class TestMemoryAgent:
    """Integration tests for create_memory_agent and the full agent loop."""

    def test_agent_creation_mock_mode(self):
        """create_memory_agent returns a (graph, session_id) tuple."""
        result = create_memory_agent(verbose=False)
        assert isinstance(result, tuple)
        assert len(result) == 2
        agent_graph, session_id = result
        assert agent_graph is not None
        assert isinstance(session_id, str)
        assert len(session_id) > 0

    def test_agent_has_correct_config(self):
        """MemoryDomainConfig has the expected domain config."""
        config = MemoryDomainConfig("infrastructure")
        assert config.name == "memory_infrastructure"
        assert len(config.tool_names) == 6
        assert "store_session_turn" not in config.tool_names
        assert "get_session_context" not in config.tool_names

    def test_agent_creation_custom_domain(self):
        """create_memory_agent works with a custom base_domain."""
        _, session_id = create_memory_agent(base_domain="repository", verbose=False)
        assert isinstance(session_id, str)

    def test_agent_creation_invalid_domain_raises(self):
        """create_memory_agent propagates ValueError for invalid base_domain."""
        with pytest.raises(ValueError):
            create_memory_agent(base_domain="", verbose=False)

    @live_bedrock
    def test_agent_invocation_returns_messages(self):
        """Agent invocation with session config returns a dict with 'messages' key."""
        agent_graph, session_id = create_memory_agent(verbose=False)
        result = agent_graph.invoke(
            {"messages": [("user", "hello")]},
            config={"configurable": {"thread_id": session_id}},
        )
        assert "messages" in result
        assert len(result["messages"]) > 0

    @live_bedrock
    def test_session_continuity(self):
        """Second turn resolves a vague reference from the first turn."""
        agent_graph, session_id = create_memory_agent(verbose=False)
        config = {"configurable": {"thread_id": session_id}}

        # First turn
        r1 = agent_graph.invoke(
            {"messages": [("user", "Is the notification-svc issue resolved?")]},
            config=config,
        )
        assert "messages" in r1

        # Second turn — "it" should resolve to notification-svc from context
        r2 = agent_graph.invoke(
            {"messages": [("user", "What was the root cause of it?")]},
            config=config,
        )
        assert "messages" in r2
        final = r2["messages"][-1].content.lower()
        # Agent should reference notification-svc or REDIS_URL from prior context
        assert any(t in final for t in ["notification", "redis", "env", "variable", "memory"])

    @live_bedrock
    def test_full_loop_references_memory_source(self):
        """Full loop test produces a response that references at least one memory source."""
        agent_graph, session_id = create_memory_agent(verbose=False)
        result = agent_graph.invoke(
            {"messages": [("user", "Is the notification-svc issue from yesterday resolved?")]},
            config={"configurable": {"thread_id": session_id}},
        )
        messages = result.get("messages", [])
        assert len(messages) > 0

        final_content = messages[-1].content.lower()
        memory_tokens = ["episodic", "procedural", "graph", "memory"]
        assert any(token in final_content for token in memory_tokens), (
            f"Expected at least one of {memory_tokens} in response, got:\n{final_content}"
        )

    def test_memory_domain_config_tool_names(self):
        """MemoryDomainConfig has exactly the six expected tool names."""
        config = MemoryDomainConfig()
        expected = {
            "store_memory",
            "recall_semantic_memory",
            "store_relationship",
            "query_relationship_graph",
            "get_blast_radius",
            "consolidate_session",
        }
        assert set(config.tool_names) == expected

    def test_memory_domain_config_pii_entities(self):
        """MemoryDomainConfig guardrail has exactly the four PII entities."""
        config = MemoryDomainConfig()
        assert set(config.guardrail_policy.pii_entities) == {
            "NAME", "EMAIL", "PHONE", "AWS_ACCESS_KEY"
        }
