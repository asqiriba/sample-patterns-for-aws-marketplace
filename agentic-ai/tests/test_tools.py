"""
tests/test_tools.py
===================
Tests for Module 1 tools and agent construction.
All tests run in mock mode — no AWS credentials required.

Run:
    AGENT_MOCK_AWS=true pytest tests/test_tools.py -v
"""

from __future__ import annotations

import json
import os
import sys

import pytest

os.environ["AGENT_MOCK_AWS"] = "true"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Tool unit tests
# ---------------------------------------------------------------------------

class TestListAwsResources:
    def test_ecs_returns_services(self) -> None:
        from module1.tools.aws_tools import list_aws_resources
        r = json.loads(list_aws_resources("ecs", "us-east-1"))
        assert r["data"]["service_type"] == "ecs"
        assert r["data"]["count"] == 4
        assert isinstance(r["data"]["resources"], list)

    def test_ec2_returns_instances(self) -> None:
        from module1.tools.aws_tools import list_aws_resources
        r = json.loads(list_aws_resources("ec2", "us-east-1"))
        assert r["data"]["count"] == 4

    def test_rds_returns_databases(self) -> None:
        from module1.tools.aws_tools import list_aws_resources
        r = json.loads(list_aws_resources("rds", "us-east-1"))
        assert r["data"]["count"] == 2

    def test_lambda_returns_functions(self) -> None:
        from module1.tools.aws_tools import list_aws_resources
        r = json.loads(list_aws_resources("lambda", "us-east-1"))
        assert r["data"]["count"] == 3

    def test_unknown_type_returns_error(self) -> None:
        from module1.tools.aws_tools import list_aws_resources
        r = json.loads(list_aws_resources("unknown", "us-east-1"))
        assert "error" in r["data"]

    def test_mock_flag_set(self) -> None:
        from module1.tools.aws_tools import list_aws_resources
        r = json.loads(list_aws_resources("ecs", "us-east-1"))
        assert r["mock_mode"] is True


class TestDescribeResource:
    def test_known_service_returns_detail(self) -> None:
        from module1.tools.aws_tools import describe_resource
        r = json.loads(describe_resource("ecs", "notification-svc", "us-east-1"))
        d = r["data"]
        assert d["name"] == "notification-svc"
        assert "recent_events" in d
        assert len(d["recent_events"]) > 0

    def test_unknown_service_returns_error(self) -> None:
        from module1.tools.aws_tools import describe_resource
        r = json.loads(describe_resource("ecs", "does-not-exist", "us-east-1"))
        assert "error" in r["data"]
        assert "hint" in r["data"]


class TestCheckResourceHealth:
    def test_healthy_service(self) -> None:
        from module1.tools.aws_tools import check_resource_health
        r = json.loads(check_resource_health("ecs", "api-gateway-svc", "us-east-1"))
        assert r["data"]["health"] == "healthy"
        assert len(r["data"]["findings"]) > 0
        assert r["data"]["recommendations"] == []

    def test_degraded_service(self) -> None:
        from module1.tools.aws_tools import check_resource_health
        r = json.loads(check_resource_health("ecs", "notification-svc", "us-east-1"))
        assert r["data"]["health"] == "degraded"
        assert len(r["data"]["recommendations"]) > 0

    def test_single_az_rds_is_degraded(self) -> None:
        from module1.tools.aws_tools import check_resource_health
        r = json.loads(check_resource_health("rds", "reporting-mysql", "us-east-1"))
        assert r["data"]["health"] == "degraded"


class TestGetEnvironmentSummary:
    def test_returns_all_service_types(self) -> None:
        from module1.tools.aws_tools import get_environment_summary
        r = json.loads(get_environment_summary("us-east-1"))
        d = r["data"]
        assert "ecs" in d["services"]
        assert "ec2" in d["services"]
        assert "rds" in d["services"]
        assert "lambda" in d["services"]

    def test_overall_health_is_degraded(self) -> None:
        from module1.tools.aws_tools import get_environment_summary
        r = json.loads(get_environment_summary("us-east-1"))
        assert r["data"]["overall_health"] == "degraded"

    def test_action_items_present(self) -> None:
        from module1.tools.aws_tools import get_environment_summary
        r = json.loads(get_environment_summary("us-east-1"))
        assert len(r["data"]["action_items"]) > 0


class TestRequestHumanReview:
    def test_creates_ticket(self, capsys) -> None:
        from module1.tools.aws_tools import request_human_review
        r = json.loads(request_human_review(
            issue_summary="Test issue",
            urgency="high",
            full_context="context details",
            recommended_action="restart the service",
        ))
        assert r["data"]["status"] == "PENDING_HUMAN_REVIEW"
        assert r["data"]["ticket_id"].startswith("INFRA-")
        assert r["data"]["urgency"] == "HIGH"

    def test_normalises_invalid_urgency(self) -> None:
        from module1.tools.aws_tools import request_human_review
        r = json.loads(request_human_review(
            issue_summary="x", urgency="INVALID", full_context="x", recommended_action="x"
        ))
        assert r["data"]["urgency"] == "MEDIUM"

    def test_console_output(self, capsys) -> None:
        from module1.tools.aws_tools import request_human_review
        request_human_review("x", "critical", "ctx", "fix it")
        captured = capsys.readouterr()
        assert "HUMAN REVIEW REQUIRED" in captured.out
        assert "CRITICAL" in captured.out


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_five_tools_registered(self) -> None:
        from module1.tools.aws_tools import ALL_TOOLS
        assert len(ALL_TOOLS) == 5

    def test_all_tools_have_docstrings(self) -> None:
        from module1.tools.aws_tools import ALL_TOOLS
        for t in ALL_TOOLS:
            assert t.__doc__ and len(t.__doc__.strip()) > 30, (
                f"Tool {t.__name__} needs a meaningful docstring — "
                "the LLM reads it to understand what the tool does."
            )

    def test_tool_names(self) -> None:
        from module1.tools.aws_tools import ALL_TOOLS
        names = {t.__name__ for t in ALL_TOOLS}
        assert names == {
            "list_aws_resources",
            "describe_resource",
            "check_resource_health",
            "get_environment_summary",
            "request_human_review",
        }


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------

class TestModelConfig:
    def test_bedrock_model_instantiates(self) -> None:
        pytest.importorskip("strands", reason="strands not installed")
        from module1.config.models import get_bedrock_model
        # Should not raise (model object creation doesn't call AWS)
        model = get_bedrock_model()
        assert model is not None

    def test_hf_model_needs_endpoint_arn(self) -> None:
        pytest.importorskip("strands", reason="strands not installed")
        from module1.config.models import get_hf_bedrock_model
        # Passing an ARN should work (no AWS call at construction time)
        model = get_hf_bedrock_model(endpoint_arn="arn:aws:sagemaker:us-east-1:123456789012:endpoint/test")
        assert model is not None

    def test_provider_info_has_required_keys(self) -> None:
        from module1.config.models import PROVIDER_INFO
        for key, info in PROVIDER_INFO.items():
            for field in ("name", "vendor", "access", "free_trial"):
                assert field in info, f"PROVIDER_INFO['{key}'] missing field '{field}'"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_system_prompt_defines_constraints(self) -> None:
        from module1.agent import SYSTEM_PROMPT
        assert len(SYSTEM_PROMPT) > 400
        assert "request_human_review" in SYSTEM_PROMPT

    def test_system_prompt_defines_observation_mode(self) -> None:
        from module1.agent import SYSTEM_PROMPT
        assert "OBSERVE" in SYSTEM_PROMPT or "observe" in SYSTEM_PROMPT.lower()

    def test_system_prompt_defines_tool_usage(self) -> None:
        from module1.agent import SYSTEM_PROMPT
        # Should mention tools by name so the agent knows when to use them
        assert "list_aws_resources" in SYSTEM_PROMPT
        assert "get_environment_summary" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Agent construction (no LLM calls)
# ---------------------------------------------------------------------------

class TestAgentConstruction:
    def test_create_agent_returns_agent(self) -> None:
        pytest.importorskip("strands", reason="strands not installed")
        from module1.agent import create_agent
        try:
            agent = create_agent(verbose=False)
            assert agent is not None
        except Exception as exc:
            # Acceptable if Bedrock credentials aren't available in test env
            if any(w in str(exc).lower() for w in ("credential", "auth", "access")):
                pytest.skip(f"AWS credentials not available: {exc}")
            raise


# ---------------------------------------------------------------------------
# Integration tests (require live AWS — skip by default)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="class")
def agent():
    """One real Strands agent shared by the TestIntegration tests."""
    os.environ.pop("AGENT_MOCK_AWS", None)
    from module1.agent import create_agent
    return create_agent(verbose=False)


@pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS", "").lower() != "true",
    reason="Set RUN_INTEGRATION_TESTS=true and AGENT_MOCK_AWS=false to run",
)
class TestIntegration:
    """
    End-to-end tests against the real Strands agent + Bedrock.

    Requirements:
        - AWS credentials configured
        - Bedrock access to Claude Sonnet 4.6 (us.anthropic.claude-sonnet-4-6)
        - RUN_INTEGRATION_TESTS=true
        - AGENT_MOCK_AWS=false (or unset)
    """

    def test_simple_list_query(self, agent) -> None:
        response = agent("List ECS services in us-east-1")
        assert response is not None
        assert len(str(response)) > 10

    def test_health_check_query(self, agent) -> None:
        response = agent("Check the health of notification-svc in us-east-1")
        text = str(response).lower()
        assert any(w in text for w in ("health", "running", "desired", "degraded", "critical"))
