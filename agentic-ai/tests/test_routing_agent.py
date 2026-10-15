"""
tests/test_routing_agent.py
============================
Tests for routing agent intent classification.

The routing agent has no mock mode: it always makes one Bedrock call. The
offline section replaces the model factory with LangChain's
``FakeListChatModel`` so the parsing, URL mapping and threshold logic can be
tested without AWS. The live section is the original Bedrock suite and only
runs when ``RUN_INTEGRATION_TESTS=true``.

Run:
    pytest tests/test_routing_agent.py -v                                   # offline
    RUN_INTEGRATION_TESTS=true pytest tests/test_routing_agent.py -k live -v  # live Bedrock
"""

import json
import os

import pytest

# Set mock mode
os.environ.setdefault("AGENT_MOCK_REPO", "true")


def _classification(category, target_agent, confidence, **extra):
    """Build a classification JSON string shaped like the routing prompt asks for."""
    return json.dumps({
        "category": category,
        "confidence": confidence,
        "reasoning": "test fixture",
        "clarifying_questions": [],
        "target_agent": target_agent,
        **extra,
    })


@pytest.fixture
def fake_model(monkeypatch):
    """Patch the routing agent's model factory with a scripted fake chat model.

    Call the returned function with the list of raw responses the model should
    return, in order.
    """
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from routing_agent import agent as routing

    def _install(responses):
        model = FakeListChatModel(responses=list(responses))
        monkeypatch.setattr(routing, "get_chat_bedrock_model", lambda **_kwargs: model)
        return model

    return _install


# ---------------------------------------------------------------------------
# Offline unit tests (no AWS)
# ---------------------------------------------------------------------------

class TestOfflineClassification:
    def test_parses_plain_json(self, fake_model):
        from routing_agent.agent import classify_intent

        fake_model([_classification("repository_analysis", "module2", 0.95)])
        result = classify_intent("Analyze the repository at /home/user/myapp", verbose=False)

        assert result["category"] == "repository_analysis"
        assert result["target_agent"] == "module2"
        assert result["confidence"] == 0.95
        assert result["reasoning"] == "test fixture"

    def test_parses_fenced_json(self, fake_model):
        from routing_agent.agent import classify_intent

        body = _classification("infrastructure_generation", "module3", 0.9)
        fake_model([f"Here is the classification:\n```json\n{body}\n```\n"])
        result = classify_intent("Generate CDK code", verbose=False)

        assert result["category"] == "infrastructure_generation"
        assert result["target_agent"] == "module3"

    @pytest.mark.parametrize(
        ("category", "target_agent"),
        [
            ("aws_infrastructure", "module1"),
            ("repository_analysis", "module2"),
            ("infrastructure_generation", "module3"),
        ],
    )
    def test_target_url_comes_from_agent_urls(self, fake_model, category, target_agent):
        from routing_agent.agent import AGENT_URLS, classify_intent

        fake_model([_classification(category, target_agent, 0.9)])
        result = classify_intent("anything", verbose=False)

        assert result["target_url"] == AGENT_URLS[target_agent]

    def test_agent_url_defaults(self):
        from routing_agent.agent import AGENT_URLS

        expected = {
            "module1": "http://localhost:8080",
            "module2": "http://localhost:8081",
            "module3": "http://localhost:8082",
        }
        for name, url in expected.items():
            if os.getenv(f"{name.upper()}_URL"):
                continue  # overridden by the environment
            assert AGENT_URLS[name] == url

    def test_unknown_target_agent_gets_empty_url(self, fake_model):
        from routing_agent.agent import classify_intent

        fake_model([_classification("unsupported", "future", 0.8)])
        result = classify_intent("Deploy to Kubernetes", verbose=False)

        assert result["target_url"] == ""

    @pytest.mark.parametrize(
        ("confidence", "action"),
        [(0.8, "route"), (0.6, "clarify"), (0.3, "reject")],
    )
    def test_route_request_thresholds(self, fake_model, confidence, action):
        from routing_agent.agent import route_request

        fake_model([_classification("repository_analysis", "module2", confidence)])
        result = route_request("Analyze my repository", verbose=False)

        assert result["suggested_action"] == action
        assert result["category_info"]["target_agent"] == "module2"

    def test_parse_failure_falls_back_to_unknown(self, fake_model):
        from routing_agent.agent import classify_intent, route_request

        fake_model(["I am not JSON"])
        result = classify_intent("???", verbose=False)

        assert result["category"] == "unknown"
        assert result["confidence"] == 0.0
        assert result["target_url"] == ""
        assert result["raw_response"] == "I am not JSON"

        fake_model(["still not JSON"])
        assert route_request("???", verbose=False)["suggested_action"] == "reject"

    def test_classify_batch_keeps_length_and_order(self, fake_model):
        from routing_agent.agent import classify_batch

        fake_model([
            _classification("repository_analysis", "module2", 0.9),
            _classification("infrastructure_generation", "module3", 0.9),
            _classification("aws_infrastructure", "module1", 0.9),
        ])
        results = classify_batch(
            ["Analyze repository", "Generate CDK for ECS", "Check AWS health"],
            verbose=False,
        )

        assert len(results) == 3
        assert [r["target_agent"] for r in results] == ["module2", "module3", "module1"]


# ---------------------------------------------------------------------------
# Live Bedrock tests (opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS", "").lower() != "true",
    reason="live Bedrock: set RUN_INTEGRATION_TESTS=true and AWS credentials",
)
class TestLiveClassification:
    def test_classify_repository_analysis(self):
        """Test classification of repository analysis requests."""
        from routing_agent.agent import classify_intent

        request = "Analyze the repository at /home/user/myapp"

        result = classify_intent(request, verbose=False)

        assert result["category"] == "repository_analysis"
        assert result["target_agent"] == "module2"
        assert result["confidence"] > 0.7

    def test_classify_infrastructure_generation(self):
        """Test classification of infrastructure generation requests."""
        from routing_agent.agent import classify_intent

        request = "Generate CDK code for a web application with PostgreSQL"

        result = classify_intent(request, verbose=False)

        assert result["category"] == "infrastructure_generation"
        assert result["target_agent"] == "module3"
        assert result["confidence"] > 0.7

    def test_classify_aws_infrastructure(self):
        """Test classification of AWS infrastructure monitoring requests."""
        from routing_agent.agent import classify_intent

        request = "Check the health of my ECS services in us-east-1"

        result = classify_intent(request, verbose=False)

        assert result["category"] == "aws_infrastructure"
        assert result["target_agent"] == "module1"
        assert result["confidence"] > 0.7

    def test_route_request_high_confidence(self):
        """Test routing with high confidence classification."""
        from routing_agent.agent import route_request

        request = "Analyze my repository"

        result = route_request(request, verbose=False)

        assert result["suggested_action"] == "route"
        assert result["confidence"] >= 0.7

    def test_classify_batch(self):
        """Test batch classification of multiple requests."""
        from routing_agent.agent import classify_batch

        requests = [
            "Analyze repository at /path/to/repo",
            "Generate CDK for ECS",
            "Check AWS health",
        ]

        results = classify_batch(requests, verbose=False)

        assert len(results) == 3
        assert all("category" in r for r in results)
        assert all("confidence" in r for r in results)

    def test_classification_includes_target_url(self):
        """Test that classification includes target URL."""
        from routing_agent.agent import classify_intent

        request = "Generate infrastructure"

        result = classify_intent(request, verbose=False)

        assert "target_url" in result
        assert result["target_url"] != ""

    def test_classification_response_structure(self):
        """Test that classification response has expected structure."""
        from routing_agent.agent import classify_intent

        request = "Test request"

        result = classify_intent(request, verbose=False)

        required_keys = ["category", "confidence", "reasoning", "target_agent", "target_url"]
        for key in required_keys:
            assert key in result
