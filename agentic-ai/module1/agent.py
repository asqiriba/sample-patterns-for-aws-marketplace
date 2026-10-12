"""
agent.py
========
Core AWS Infrastructure Agent — Module 1.

This file is the heart of the skeleton. It assembles all three layers
(Reasoning, Orchestration, Tools) into a working Strands Agent.

MODULE 1 CONCEPTS IMPLEMENTED
-------------------------------

  REASONING LAYER
  ───────────────
  Claude Sonnet 4.6 via Amazon Bedrock (Anthropic on AWS Marketplace).
  Optionally replaceable with a Hugging Face model from Bedrock Marketplace
  — the rest of the file is unchanged. This is the model-agnostic property
  of the Strands framework that the module highlights.

  ORCHESTRATION LAYER
  ───────────────────
  • Strands Agent     : drives the Think → Act → Observe loop automatically
  • System prompt     : defines the agent persona, scope, and hard constraints
  • Conversation manager : context window management (sliding window)
  • Callback handler  : lightweight observability — prints each loop step to
                        console so workshop attendees can watch the loop run

  TOOLS LAYER
  ──────────
  • list_aws_resources     — ECS / EC2 / RDS / Lambda listings
  • describe_resource      — detailed drill-down on a specific resource
  • check_resource_health  — opinionated health assessment
  • get_environment_summary — cross-service overview
  • request_human_review   — human-in-the-loop escalation

  All Module 1 tools are READ-ONLY. The only action path is
  request_human_review, which creates a structured escalation record.

CONTEXT WINDOW MANAGEMENT
--------------------------
The SlidingWindowConversationManager keeps the last N conversation turns
in the context window and discards older ones. This implements the
short-term memory concept from the slides.

  - window_size=10 → ~5 back-and-forth exchanges kept
  - After turn 11, turn 1 is dropped (sliding window)
  - Long-term memory across sessions comes in Module 7

USAGE
-----
    # Default (Claude Sonnet 4.6 via Bedrock, verbose, 10-turn context window)
    from agent import create_agent
    agent = create_agent()
    response = agent("Give me a health summary of us-east-1")
    print(response)

    # Hugging Face alternative (same agent logic, different model)
    import os
    from agent import create_agent
    agent = create_agent(hf_endpoint_arn=os.environ["HF_ENDPOINT_ARN"])
    response = agent("List ECS services in us-east-1")
"""

from __future__ import annotations

import os
from typing import Any

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager

from module1.config.models import get_bedrock_model, get_hf_bedrock_model
from module1.tools.aws_tools import ALL_TOOLS


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
# The system prompt is the agent's constitution. It defines:
#   - What the agent IS (role + persona)
#   - What it CAN do (capabilities)
#   - What it CANNOT do (explicit scope constraints)
#   - HOW it should reason and respond
#
# Good system prompts are precise, not vague. The constraints here are
# deliberate — they enforce the Phase 1 (Assist) adoption pattern.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an AWS Infrastructure Agent working with an engineering \
team that builds and operates microservices on AWS.

## Your Role in Module 1

You are operating in OBSERVE AND ANALYSE mode. Your job is to:

1. **Observe** — use your tools to retrieve the current state of AWS infrastructure.
   Never guess or rely on prior knowledge about what is deployed. Always call a tool.

2. **Analyse** — identify issues, risks, and anomalies in what you observe.

3. **Reason** — explain your findings clearly, citing the data returned by tools.

4. **Recommend** — propose specific, concrete next steps.

5. **Escalate** — use the `request_human_review` tool for any action that would
   modify infrastructure. Do not describe what you "would" do — raise a formal
   review request with your full analysis so a human can act on it.

## Hard Constraints (Module 1)

- You have NO ability to create, modify, or delete any AWS resource directly.
- ALL proposed write operations MUST go through `request_human_review`.
- If you identify something that needs fixing, your job ends at raising the review
  request — not at actually fixing it.

## Tool Usage

- Start broad: use `get_environment_summary` for overview questions.
- Drill in: use `list_aws_resources` then `describe_resource` for specifics.
- For health questions: use `check_resource_health` which returns a structured verdict.
- Escalate: use `request_human_review` with complete context when action is needed.
- Always pass `region` explicitly when the user mentions one.

## Response Format

Structure responses as:
  **Summary**: one-sentence answer
  **Findings**: bullet list of what you observed (cite tool output)
  **Recommendations**: concrete next steps (or "None — no action required")

Keep technical responses factual and concise. Use severity language:
  critical (immediate action) / degraded (investigate soon) / healthy (no action)
"""


# ---------------------------------------------------------------------------
# Callback Handler (observability for the workshop)
# ---------------------------------------------------------------------------

class LoopObserver:
    """
    Callback handler that prints each step of the Think → Act → Observe loop.

    This makes the agentic loop visible during the workshop demo.
    In production (Module 12) this is replaced by structured CloudWatch
    logging and X-Ray / OpenTelemetry tracing.

    Strands calls this object at key lifecycle events. We inspect the
    event dict and print human-readable summaries.
    """

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.tool_calls: list[dict[str, Any]] = []
        self._step = 0

    def __call__(self, **event: Any) -> None:
        if not self.verbose:
            return

        event_type = (event.get("event_type") or "").lower()

        # Model decided to call a tool
        if "tool_use" in event_type and "start" in event_type:
            self._step += 1
            name = event.get("tool_name", "?")
            inp = event.get("tool_input", {})
            self.tool_calls.append({"tool": name, "input": inp})
            print(f"\n  🔧 [Step {self._step}] ACT → {name}(", end="")
            parts = [f"{k}={repr(v)}" for k, v in inp.items()]
            print(", ".join(parts) + ")")

        # Tool returned its result
        elif "tool_use" in event_type and ("end" in event_type or "result" in event_type):
            name = event.get("tool_name", "?")
            print(f"  ✓  OBSERVE ← {name} returned (added to context)")

        # Model is reasoning
        elif event_type in ("on_llm_start", "before_model_call"):
            print(f"\n  🧠 THINK  (reasoning over context...)")


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

def create_agent(
    *,
    hf_endpoint_arn: str | None = None,
    verbose: bool = True,
    window_size: int = 10,
    region: str | None = None,
) -> Agent:
    """
    Assemble and return the Module 1 AWS Infrastructure Agent.

    This function is the integration point of all three layers:

      Reasoning    → BedrockModel (Claude Sonnet 4.6 or HF endpoint)
      Orchestration→ Strands Agent + SlidingWindowConversationManager + LoopObserver
      Tools        → ALL_TOOLS (list / describe / health / summary / escalate)

    Parameters
    ----------
    hf_endpoint_arn : str, optional
        SageMaker endpoint ARN for a Hugging Face model deployed through
        Bedrock Marketplace. When provided, uses HF instead of Claude.
        All other agent configuration remains identical — demonstrating
        model-agnostic architecture.
    verbose : bool
        Print Think → Act → Observe loop steps. Default True for demos.
    window_size : int
        Number of conversation turns kept in context. Default 10.
    region : str, optional
        AWS region override. Falls back to AWS_REGION env var.

    Returns
    -------
    strands.Agent
    """
    aws_region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

    # ── REASONING LAYER ──────────────────────────────────────────────────────
    if hf_endpoint_arn:
        # Hugging Face model via Bedrock Marketplace — same BedrockModel class,
        # different model_id (the SageMaker endpoint ARN).
        model = get_hf_bedrock_model(endpoint_arn=hf_endpoint_arn, region=aws_region)
        print(f"  [Agent] Using Hugging Face model via Bedrock Marketplace")
        print(f"          Endpoint ARN: {hf_endpoint_arn[:60]}...")
    else:
        # Default: Claude Sonnet 4.6 via Bedrock (Anthropic on AWS Marketplace)
        model = get_bedrock_model(region=aws_region)
        print(f"  [Agent] Using Claude Sonnet 4.6 via Amazon Bedrock (Anthropic)")

    # ── CONTEXT WINDOW MANAGEMENT ────────────────────────────────────────────
    # SlidingWindowConversationManager = the concrete implementation of
    # "short-term memory" discussed in the Module 1 slides.
    conversation_manager = SlidingWindowConversationManager(window_size=window_size)

    # ── LOOP OBSERVER (observability) ────────────────────────────────────────
    observer = LoopObserver(verbose=verbose)

    # ── ORCHESTRATION LAYER: assemble the agent ───────────────────────────────
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=ALL_TOOLS,                            # Tools Layer
        conversation_manager=conversation_manager,   # Short-term memory
        callback_handler=observer,                   # Observability
    )

    return agent
