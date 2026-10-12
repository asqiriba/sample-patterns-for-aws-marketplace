# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
tests/test_module4_orchestrator.py
===================================
Offline smoke tests for the Module 4 multi-agent orchestrator.

Covers the MCP tool registry, the HTTP (A2A) client in mock mode, the
orchestration tool registry, and orchestrator construction. The orchestrator
test builds a real ChatBedrock without invoking it, which guards tool binding
on newer langchain-aws releases. No AWS credentials or agent servers needed.

Run (module4 venv):
    AGENT_MOCK_MODE=true pytest tests/test_module4_orchestrator.py -v
"""
from __future__ import annotations

import json
import os
import socket

# module4/protocols/*.py read AGENT_MOCK_MODE at import time.
os.environ.setdefault("AGENT_MOCK_MODE", "true")

import pytest

EXPECTED_MCP_TOOLS = {"mcp_analyze_requirements", "mcp_generate_cdk", "mcp_validate_cdk"}
EXPECTED_HTTP_TOOLS = {
    "call_infrastructure_agent",
    "call_repository_agent",
    "run_sequential_pipeline",
    "run_parallel_fanout",
    "synthesize_results",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail fast if anything in these tests tries to open a network connection."""
    def _blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


class TestMCPRegistry:
    def test_registry_lists_three_module3_tools(self):
        from module4.protocols.mcp_protocol import MCPToolRegistry

        tools = MCPToolRegistry(verbose=False).list_tools()

        assert {t["name"] for t in tools} == EXPECTED_MCP_TOOLS
        assert all(t["source_agent"] == "module3" for t in tools)
        assert all(t["schema"]["type"] == "object" for t in tools)

    def test_langchain_wrappers_match_registry(self):
        from module4.protocols.mcp_protocol import MCP_TOOLS

        assert {t.name for t in MCP_TOOLS} == EXPECTED_MCP_TOOLS

    def test_mock_call_returns_json(self):
        from module4.protocols import mcp_protocol
        from module4.protocols.mcp_protocol import mcp_generate_cdk

        assert mcp_protocol._MOCK is True
        data = json.loads(mcp_generate_cdk.invoke({"stack_type": "rds"}))

        assert data["tool"] == "mcp_generate_cdk"
        assert data["source_agent"] == "module3"
        assert data["result"]["stack_type"] == "rds"
        assert data["result"]["syntax_valid"] is True

    def test_unknown_tool_returns_error(self):
        from module4.protocols.mcp_protocol import MCPToolRegistry

        result = MCPToolRegistry(verbose=False).invoke_tool("mcp_unknown", {})
        assert "error" in result


class TestA2AClientMock:
    @pytest.fixture(autouse=True)
    def forbid_http(self, monkeypatch):
        from module4.protocols.a2a_protocol import A2AClient

        def _fail(*_args, **_kwargs):
            raise AssertionError("A2AClient made an HTTP call in mock mode")

        monkeypatch.setattr(A2AClient, "_http_call", _fail)

    def test_module1_health_check_is_mocked(self):
        from module4.protocols import a2a_protocol
        from module4.protocols.a2a_protocol import A2AClient

        assert a2a_protocol._MOCK is True
        result = A2AClient(verbose=False).call_agent("module1", "health_check")

        assert result["agent"] == "module1"
        assert result["task"] == "health_check"
        assert "error" not in result["data"]

    def test_module2_analyze_repository_is_mocked(self):
        from module4.protocols.a2a_protocol import A2AClient

        result = A2AClient(verbose=False).call_agent("module2", "analyze_repository")

        assert result["agent"] == "module2"
        assert {"repository", "applications", "summary"} <= set(result["data"])

    def test_unknown_agent_returns_error(self):
        from module4.protocols.a2a_protocol import A2AClient

        result = A2AClient(verbose=False).call_agent("module99", "health_check")
        assert "error" in result


class TestOrchestrationTools:
    def test_five_http_tools(self):
        from module4.tools.orchestration_tools import ALL_TOOLS

        assert len(ALL_TOOLS) == 5
        assert {t.name for t in ALL_TOOLS} == EXPECTED_HTTP_TOOLS


class TestCreateOrchestrator:
    def test_builds_compiled_graph_with_all_eight_tools(self, monkeypatch):
        from langchain_aws import ChatBedrock
        from langgraph.graph.state import CompiledStateGraph

        import module4.agent as m4_agent

        built = {}
        real_factory = m4_agent.get_chat_bedrock_model

        def _capture(**kwargs):
            built["model"] = real_factory(**kwargs)
            return built["model"]

        monkeypatch.setattr(m4_agent, "get_chat_bedrock_model", _capture)

        graph = m4_agent.create_orchestrator(verbose=False, region="us-east-1")

        assert isinstance(graph, CompiledStateGraph)
        assert isinstance(built["model"], ChatBedrock)
        assert built["model"].model_id == "us.anthropic.claude-sonnet-4-6"
        tool_node = graph.nodes["tools"].bound
        assert set(tool_node.tools_by_name) == EXPECTED_HTTP_TOOLS | EXPECTED_MCP_TOOLS

    def test_without_mcp_binds_http_tools_only(self):
        from module4.agent import create_orchestrator

        graph = create_orchestrator(verbose=False, include_mcp=False)

        assert set(graph.nodes["tools"].bound.tools_by_name) == EXPECTED_HTTP_TOOLS
