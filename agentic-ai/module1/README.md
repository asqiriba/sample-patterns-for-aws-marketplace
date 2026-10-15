# Module 1: AWS Infrastructure Agent

Strands-based agent that observes and analyzes AWS infrastructure with five
read-only tools, and escalates to a human instead of acting on its own.

## Overview

Module 1 introduces the building blocks every later module reuses: the three
layers of an agent (reasoning, orchestration, tools), the Think → Act → Observe
loop, short-term memory in the context window, the human-in-the-loop pattern,
and a model-agnostic design where swapping the model is a one-line change.

## Quick Start

Run from `agentic-ai/` in the core environment (`./INSTALL.sh`, then
`source .venv/bin/activate`; see the [environment matrix](../README.md#environments)).

```bash
# Mock mode: simulated AWS data, live Amazon Bedrock calls
AGENT_MOCK_AWS=true python demos/module1_demo.py

# Run a specific section (1-6)
AGENT_MOCK_AWS=true python demos/module1_demo.py --section 4

# Run tests (offline)
AGENT_MOCK_AWS=true pytest tests/test_tools.py -v
```

The demo calls Claude Sonnet 4.6 on Amazon Bedrock, so it needs AWS
credentials even in mock mode. `AGENT_MOCK_AWS=true` only replaces the AWS
resource data the tools return. The two integration tests in
`tests/test_tools.py` call AWS and run only with `RUN_INTEGRATION_TESTS=true`.

## Architecture

**Framework**: AWS Strands Agents
**Model Interface**: `BedrockModel` (`module1/config/models.py`)
**Execution Loop**: Strands `Agent` with a `LoopObserver` callback that prints each Think → Act → Observe step
**Memory**: `SlidingWindowConversationManager` (short-term, in the context window)
**Tools**: 5 read-only AWS tools

## Key Concepts

- Three layers (reasoning / orchestration / tools)
- Think → Act → Observe loop with `LoopObserver`
- Context window and short-term memory
- Human-in-the-loop pattern
- Model-agnostic architecture

## Tools Reference

All Module 1 tools are **read-only**. The only action path is `request_human_review`.

| Tool | Purpose | Slide Concept |
|---|---|---|
| `list_aws_resources(service_type, region)` | ECS / EC2 / RDS / Lambda listings | Grounding: agent sees current state |
| `describe_resource(service_type, resource_name, region)` | Detailed drill-down | Multi-step reasoning |
| `check_resource_health(service_type, resource_name, region)` | Structured health verdict | Observation |
| `get_environment_summary(region)` | Cross-service overview | Data synthesis |
| `request_human_review(...)` | Escalation ticket | Human-in-the-loop |

### Mock data

With `AGENT_MOCK_AWS=true` the tools return realistic simulated data including:
- **`api-gateway-svc`** → healthy (3/3 tasks running)
- **`notification-svc`** → degraded (1/2 tasks, EssentialContainerExited event) ← triggers HITL
- **`reporting-mysql`** → degraded (Single-AZ RDS, no failover)
- **`prod-postgres-01`** → healthy (Multi-AZ, available)

## The Model-Agnostic Property

The most important architectural property to highlight in the workshop:

```python
# module1/config/models.py

# Anthropic — Claude Sonnet 4.6 via Amazon Bedrock
model = get_bedrock_model()

# Hugging Face — open model via Bedrock Marketplace (ONE LINE change)
model = get_hf_bedrock_model(endpoint_arn="arn:aws:sagemaker:...")
```

The `create_agent()` factory in `module1/agent.py` accepts `hf_endpoint_arn` and swaps the model. **Tools, system prompt, conversation manager, and AgentCore wrapper are completely unchanged.**

## Live AWS Mode

Live mode makes read-only ECS, EC2, RDS and Lambda calls in your account. The
Bedrock model access steps (automatic enablement, the one-time Anthropic
use-case submission, IAM permissions) are in the series README:
[Amazon Bedrock model access](../README.md#amazon-bedrock-model-access).

```bash
# Run against real AWS
python demos/module1_demo.py --live

# Same agent, Hugging Face model through Amazon Bedrock Marketplace
export HF_ENDPOINT_ARN="arn:aws:sagemaker:..."
python demos/module1_demo.py --live --hf
```

`--hf` needs a Hugging Face model deployed from the Bedrock Model Catalog, which
creates a SageMaker endpoint that bills by the hour while it runs; see
[Hugging Face (via Amazon Bedrock Marketplace)](../README.md#hugging-face-via-amazon-bedrock-marketplace).

## Deploying to AgentCore Runtime

`module1/app.py` wraps the agent in a `BedrockAgentCoreApp`, the AgentCore
Runtime entrypoint. The configure, local test, deploy and invoke steps are in
the series README:
[Deploying to AgentCore Runtime (Module 1)](../README.md#deploying-to-agentcore-runtime-module-1).
To try the entrypoint locally:

```bash
AGENT_MOCK_AWS=true python -m module1.app
# → Serves on http://localhost:8080/invocations
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Give me a health summary of us-east-1"}'
```

The local server has no authentication. Run it only while you use it, and keep
it on `127.0.0.1`. With `bedrock-agentcore` installed (the core environment),
`python -m module1.app` serves the SDK's own local server; without it, a plain
HTTP fallback serves the same `/invocations` and `/ping` endpoints.

## Project Structure

```
module1/
├── agent.py              # create_agent() factory, LoopObserver, system prompt
├── app.py                # AgentCore Runtime entrypoint (port 8080)
├── config/
│   └── models.py         # BedrockModel and Hugging Face Marketplace model factories
└── tools/
    └── aws_tools.py      # 5 read-only AWS tools with mock data
```

## Demo Sections

Run `AGENT_MOCK_AWS=true python demos/module1_demo.py --section N`:

| # | Title | Key Concept |
|---|---|---|
| 1 | Architecture anatomy | Three layers in code |
| 2 | The loop | Think → Act → Observe, live |
| 3 | Multi-step reasoning | Compound query, multiple tool calls |
| 4 | Human-in-the-loop | Agent escalates, doesn't act |
| 5 | Model swap | Anthropic ↔ Hugging Face |
| 6 | Context window | Multi-turn short-term memory |

## Next Steps

- **Module 2**: the same think-act-observe loop in LangChain and LangGraph
- **Module 4**: Module 1 as a specialist agent behind an orchestrator

## License

Part of the AI Agent Learning Series on AWS.
