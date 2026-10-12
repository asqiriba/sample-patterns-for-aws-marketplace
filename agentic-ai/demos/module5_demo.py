# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#!/usr/bin/env python3
"""
demos/module5_demo.py
=====================
Live workshop demonstration for Module 5: Domain-Specific Agent Applications.

Walks through the Domain Adaptation Engine, its four levers (prompt, corpus,
tools, guardrails), and three live domain showcases: customer engagement,
geospatial intelligence, and voice interaction.

USAGE
-----
  # Mock mode: domain tools are simulated, LLM calls are real
  AGENT_MOCK_DOMAIN=true python demos/module5_demo.py

  # Run specific section
  AGENT_MOCK_DOMAIN=true python demos/module5_demo.py --section 3

SECTIONS
--------
  1   Domain specialization problem  — Generic agent fails on domain queries
  2   Domain Adaptation Engine       — DomainConfig + DomainAdapter anatomy
  3   Lever 1: Prompt engineering    — PromptLayers compose domain identity
  4   Lever 2: Knowledge corpus      — Bedrock KB grounding reduces hallucination
  5   Lever 3: Tool scoping          — ToolScoper as security boundary
  6   Lever 4: Guardrails            — GuardrailPolicy enforces policy
  7   Showcase: Customer engagement  — Live customer agent query
  8   Showcase: Geospatial           — Live geospatial agent query
  9   Showcase: Voice interaction    — Live voice agent query
  10  Domain evaluation              — Quality delta: generic vs specialized
  11  Multi-agent composition        — Domain agents plug into Module 4
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Rich output helpers
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.syntax import Syntax
    _c = Console()
    _RICH = True

    def header(text: str, color: str = "cyan") -> None:
        _c.rule(f"[bold {color}]{text}[/bold {color}]", style=color)

    def concept(text: str) -> None:
        _c.print(f"\n[bold yellow]💡 Module 5 Concept:[/bold yellow] [yellow]{text}[/yellow]")

    def user_says(text: str) -> None:
        _c.print(f"\n[bold green]USER ›[/bold green] [italic]{text}[/italic]")

    def box(title: str, body: str) -> None:
        _c.print(Panel(f"[dim]{body}[/dim]", title=f"[bold]{title}[/bold]", border_style="cyan"))

    def code_block(code: str, language: str = "python") -> None:
        syntax = Syntax(code, language, theme="monokai", line_numbers=False)
        _c.print(syntax)

    def info_list(title: str, items: list[tuple[str, str]], color: str = "cyan") -> None:
        _c.print(f"\n  [bold {color}]{title}[/bold {color}]")
        for label, desc in items:
            _c.print(f"    [bold]{label}[/bold] — {desc}")
        _c.print()

except ImportError:
    _RICH = False

    def header(text: str, color: str = "cyan") -> None:  # type: ignore[misc]
        print(f"\n{'═' * 62}\n  {text}\n{'═' * 62}")

    def concept(text: str) -> None:  # type: ignore[misc]
        print(f"\n💡 Concept: {text}")

    def user_says(text: str) -> None:  # type: ignore[misc]
        print(f"\nUSER › {text}")

    def box(title: str, body: str) -> None:  # type: ignore[misc]
        print(f"\n[ {title} ]\n{body}")

    def code_block(code: str, language: str = "python") -> None:  # type: ignore[misc]
        print(f"\n{code}\n")

    def info_list(title: str, items: list[tuple[str, str]], color: str = "cyan") -> None:  # type: ignore[misc]
        print(f"\n  {title}")
        for label, desc in items:
            print(f"    {label} — {desc}")
        print()


def pause(msg: str = "  ↵  Press Enter to continue...") -> None:
    try:
        input(msg)
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# Section 1 — Domain Specialization Problem
# ---------------------------------------------------------------------------

def section_1_specialization_problem() -> None:
    header("SECTION 1 — The Domain Specialization Problem", "cyan")

    box(
        "Generic Agent: Three Failure Modes",
        "Query 1 (Customer): 'I need a monitoring tool that integrates with AWS'\n"
        "  Generic response: 'There are many monitoring tools available. You might\n"
        "   consider CloudWatch, Datadog, or New Relic depending on your needs.'\n"
        "  Problem: No product catalog lookup, no personalization, no intent classification.\n\n"
        "Query 2 (Geospatial): 'What is the driving distance from Seattle to Portland?'\n"
        "  Generic response: 'Seattle to Portland is approximately 175 miles.'\n"
        "  Problem: Estimated from training data — no routing tool called, no live data.\n\n"
        "Query 3 (Voice): 'Transcribe the audio at s3://demo-bucket/call.wav'\n"
        "  Generic response: '## Transcription Result\\n- Speaker 1: ...\\n- Speaker 2: ...'\n"
        "  Problem: Markdown output is unreadable when spoken aloud by a TTS engine.",
    )

    info_list("What a Generic Agent Lacks", [
        ("Domain identity", "No persona, no authority, no brand voice"),
        ("Scoped tools", "Access to ALL tools — including ones it shouldn't use"),
        ("Corpus grounding", "No domain knowledge base — answers from training data only"),
        ("Guardrails", "No PII protection, no topic restrictions, no content filters"),
    ], color="red")

    concept(
        "Generic agents lack domain identity, scoped tools, and guardrails. "
        "The same LLM that answers customer queries also has access to infrastructure "
        "tools — a security and quality problem. Domain specialization solves all three."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 2 — Anatomy of the Domain Adaptation Engine
# ---------------------------------------------------------------------------

def section_2_engine_anatomy() -> None:
    header("SECTION 2 — Anatomy of the Domain Adaptation Engine", "cyan")

    code_block("""# The two core classes of the Domain Adaptation Engine

@dataclass
class DomainConfig:
    \"\"\"Declarative domain configuration — the unit of specialization.\"\"\"
    name: str                          # e.g. "customer_engagement"
    display_name: str                  # e.g. "Customer Engagement Specialist"
    prompt_layers: PromptLayers        # Lever 1: four-layer system prompt
    tool_names: list[str]              # Lever 3: scoped tool registry keys
    guardrail_policy: GuardrailPolicy  # Lever 4: Bedrock Guardrails config
    corpus_config: CorpusConfig | None # Lever 2: Bedrock Knowledge Base


class DomainAdapter:
    \"\"\"Takes a base model + DomainConfig and produces a specialized agent.\"\"\"

    def adapt(self, base_model, config: DomainConfig) -> DomainAgent:
        system_prompt = PromptBuilder().compose(config.prompt_layers)  # Lever 1
        tools = ToolScoper(self._tool_registry).select(config.tool_names)  # Lever 3
        guardrail_id = build_guardrail(config.guardrail_policy, config.name)  # Lever 4
        agent = create_react_agent(base_model, tools, prompt=system_prompt)
        return DomainAgent(agent=agent, config=config, guardrail_id=guardrail_id)
""")

    info_list("The Four Levers of Domain Adaptation", [
        ("Lever 1: Prompt", "PromptLayers compose role, knowledge, constraints, communication"),
        ("Lever 2: Corpus", "CorpusConfig wires a Bedrock Knowledge Base for RAG grounding"),
        ("Lever 3: Tools", "tool_names list restricts which tools the agent can call"),
        ("Lever 4: Guardrails", "GuardrailPolicy enforces PII, topic, and content policies"),
    ])

    box(
        "One Engine, Any Domain",
        "The same DomainAdapter.adapt() call produces:\n"
        "  • Customer Engagement Specialist  (Algolia + Personalize + Lex + Connect)\n"
        "  • Geospatial Intelligence Specialist  (Location Service + Mapbox)\n"
        "  • Voice Interaction Specialist  (Deepgram + Transcribe + Polly)\n\n"
        "Change the DomainConfig → get a completely different agent.\n"
        "The base model (Claude Sonnet 4.6 via Bedrock) never changes.",
    )

    concept("Four levers, one engine, any domain.")
    pause()


# ---------------------------------------------------------------------------
# Section 3 — Lever 1: Domain Prompt Engineering
# ---------------------------------------------------------------------------

def section_3_prompt_engineering() -> None:
    header("SECTION 3 — Lever 1: Domain Prompt Engineering", "cyan")

    code_block("""# PromptLayers: four-layer domain prompt definition
# Tools tell the model WHAT it can do.
# Guardrails enforce WHAT IS BLOCKED.
# Knowledge context fills the gap: WHEN and WHY to use each tool.
#
# Without knowledge_context, the geospatial agent might answer
# "Seattle to Portland is ~175 miles" from training data and never
# call a routing tool — because it already "knows" the answer.
# Guardrails can't block that. Tool names don't prevent it.
# Only the knowledge context instruction changes the behavior.

CUSTOMER_DOMAIN = DomainConfig(
    prompt_layers=PromptLayers(
        role="You are a Customer Engagement Specialist for the DevOps Companion "
             "platform. Your role is to help customers find the right products, "
             "resolve service issues, and ensure a positive experience.",

        knowledge_context="You have access to the product catalog via semantic "
                          "search, personalized recommendations based on customer "
                          "history, and customer intent classification. Use these "
                          "tools to provide accurate, personalized responses.",

        constraints="Never make price guarantees or commit to discounts without "
                    "authorization. Never share internal roadmap details. Always "
                    "offer escalation when customer sentiment is negative.",

        communication="Warm, professional tone. Responses under 3 sentences for "
                      "simple queries. Always acknowledge the customer's concern "
                      "before providing information.",
    ),
    ...
)
""")

    from module5.engine.domain_adapter import PromptBuilder
    from module5.domains.customer import CUSTOMER_DOMAIN

    builder = PromptBuilder()
    composed = builder.compose(CUSTOMER_DOMAIN.prompt_layers)

    print("\n  Composed system prompt (first 400 chars):")
    print(f"  {composed[:400].replace(chr(10), chr(10) + '  ')}...\n")

    info_list("What each layer does that the others can't", [
        ("Role", "Gives the agent domain identity — who it is, not just what it can do"),
        ("Knowledge Context", "Teaches WHEN and WHY to use tools — fills the gap tools and guardrails leave"),
        ("Constraints", "Prompt-level rules that shape reasoning before guardrails fire"),
        ("Communication", "Brand voice, format, length — enforced by the model, not infrastructure"),
    ])

    concept(
        "Tools say what the agent CAN do. "
        "Guardrails block what it MUST NOT do. "
        "Knowledge context teaches it WHEN and WHY — that's the gap these layers fill."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 4 — Lever 2: Knowledge Corpus Grounding
# ---------------------------------------------------------------------------

def section_4_corpus_grounding() -> None:
    header("SECTION 4 — Lever 2: Knowledge Corpus Grounding", "cyan")

    code_block("""# Algolia NeuralSearch — live product catalog search
# Index: devops-companion-catalog  |  8 products seeded

@tool
def algolia_semantic_search(query: str, index_name: str = "devops-companion-catalog") -> str:
    client = SearchClientSync(os.environ["ALGOLIA_APP_ID"], os.environ["ALGOLIA_API_KEY"])
    response = client.search_single_index(index_name, {"query": query, "hitsPerPage": 5})
    # NeuralSearch: semantic understanding + keyword precision + business rules
    # Sub-100ms p99 — fast enough for synchronous agent tool calls
""")

    box(
        "Why Algolia for Product Search?",
        "Generic vector search returns semantically similar results.\n"
        "Algolia NeuralSearch returns the RIGHT results — ranked by business relevance.\n\n"
        "  • NeuralSearch = semantic understanding + keyword precision in one call\n"
        "  • Business rules: boost new products, bury out-of-stock, pin promotions\n"
        "  • Personalization signals: re-rank results per user based on behavior\n"
        "  • Sub-100ms p99 — fast enough for synchronous agent tool calls\n"
        "  • Analytics: see what customers search for and don't find\n\n"
        "Amazon Kendra / OpenSearch handle document retrieval well.\n"
        "Algolia is purpose-built for product discovery and catalog search.\n"
        "For long-form docs (runbooks, support articles), add a Bedrock KB\n"
        "via DomainConfig.corpus_config — use both for different data types.",
    )

    print("\n  Live Algolia search — calling devops-companion-catalog now...\n")
    try:
        from module5.tools.algolia_tools import algolia_semantic_search
        import json
        result = algolia_semantic_search.invoke({"query": "observability anomaly detection"})
        data = json.loads(result)
        hits = data.get("data", {}).get("hits", [])
        print(f"  Query: 'observability anomaly detection'")
        print(f"  Results ({data['data'].get('nb_hits', 0)} hits, mock={data['mock_mode']}):")
        for h in hits[:3]:
            print(f"    • {h.get('name', h.get('objectID', '?'))} — {h.get('category', '')}")
        print()
    except Exception as exc:
        print(f"  Query: 'observability anomaly detection'")
        print(f"  Results (mock): DevOps Companion Pro, Log Analytics Pro, CloudWatch Insights Accelerator")
        print(f"  Note: {exc}\n")

    concept(
        "Algolia searched across product names, descriptions, and categories — "
        "returning ranked results from your live catalog, not from the model's training data."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 5 — Lever 3: Tool Scoping
# ---------------------------------------------------------------------------

def section_5_tool_scoping() -> None:
    header("SECTION 5 — Lever 3: Tool Scoping", "cyan")

    info_list("Tool Registry by Domain", [
        ("customer_engagement", "algolia_semantic_search, personalize_get_recommendations, lex_classify_intent, connect_escalate_to_human"),
        ("geospatial_intelligence", "location_geocode, location_reverse_geocode, location_calculate_route, location_search_nearby, mapbox_forward_geocode, mapbox_get_directions, mapbox_isochrone, mapbox_poi_search"),
        ("voice_interaction", "deepgram_transcribe, deepgram_synthesize, transcribe_audio, polly_synthesize"),
    ], color="blue")

    code_block("""# ToolScoper: selects only domain-allowed tools from the shared registry

class ToolScoper:
    def __init__(self, registry: dict[str, BaseTool]):
        self._registry = registry  # ALL tools across all domains

    def select(self, tool_names: list[str]) -> list[BaseTool]:
        \"\"\"Return only the tools this domain is allowed to use.\"\"\"
        tools = []
        for name in tool_names:
            if name not in self._registry:
                raise KeyError(name)  # Fail fast — misconfiguration is a bug
            tools.append(self._registry[name])
        return tools


# Customer agent gets 4 tools — NOT the 8 geospatial tools
customer_tools = ToolScoper(ALL_TOOLS).select([
    "algolia_semantic_search",
    "personalize_get_recommendations",
    "lex_classify_intent",
    "connect_escalate_to_human",
])
# → The customer agent CANNOT call location_calculate_route or deepgram_transcribe
""")

    box(
        "Tool Scoping as a Security Boundary",
        "Without scoping, a customer agent could call:\n"
        "  • location_calculate_route  (leaks location data)\n"
        "  • deepgram_transcribe       (processes audio it shouldn't)\n"
        "  • polly_synthesize          (generates unauthorized audio)\n\n"
        "With ToolScoper, the agent's tool list is an ALLOWLIST.\n"
        "The LLM cannot call tools that aren't in its context.\n\n"
        "This is blast radius reduction: a compromised or confused\n"
        "customer agent can only affect customer-domain resources.",
    )

    concept("Tool scoping is a security boundary — blast radius reduction.")
    pause()


# ---------------------------------------------------------------------------
# Section 6 — Lever 4: Guardrails
# ---------------------------------------------------------------------------

def section_6_guardrails() -> None:
    header("SECTION 6 — Lever 4: Guardrails", "cyan")

    code_block("""# GuardrailPolicy: Bedrock Guardrails configuration for a domain

@dataclass
class GuardrailPolicy:
    pii_handling: str = "ANONYMIZE"          # BLOCK or ANONYMIZE
    pii_entities: list[str] = field(default_factory=lambda: ["NAME", "EMAIL", "PHONE"])
    denied_topics: list[dict] = field(default_factory=list)
    content_filters: dict[str, str] = field(default_factory=dict)  # category → strength
    word_filters: list[str] = field(default_factory=list)


# Customer domain guardrail policy
customer_guardrail = GuardrailPolicy(
    pii_handling="ANONYMIZE",
    pii_entities=["NAME", "EMAIL", "PHONE", "CREDIT_DEBIT_CARD_NUMBER"],
    denied_topics=[
        {"name": "competitor_pricing",
         "definition": "Comparisons with competitor product pricing or features"},
        {"name": "internal_roadmap",
         "definition": "Unreleased product features, timelines, or internal plans"},
    ],
    content_filters={"INSULTS": "HIGH", "MISCONDUCT": "HIGH"},
)
""")

    box(
        "Mock Guardrail Check — PII Content",
        "Input:  'My name is John Smith, email john@example.com. I need help.'\n\n"
        "Guardrail action (ANONYMIZE):\n"
        "  NAME  → [PERSON]\n"
        "  EMAIL → [EMAIL_ADDRESS]\n\n"
        "Sanitized: 'My name is [PERSON], email [EMAIL_ADDRESS]. I need help.'\n\n"
        "The agent never sees the raw PII — it only processes the anonymized version.\n"
        "This happens at the Bedrock Guardrails layer, before the LLM call.",
    )

    info_list("Guardrail Layers", [
        ("PII handling", "ANONYMIZE replaces entities inline; BLOCK rejects the request"),
        ("Denied topics", "Semantic classifier blocks off-topic requests (e.g. competitor pricing)"),
        ("Content filters", "Strength levels: NONE → LOW → MEDIUM → HIGH per category"),
        ("Word filters", "Exact-match blocklist (e.g. 'password', 'secret key' for voice domain)"),
    ])

    concept("Guardrails enforce policy at the infrastructure level.")
    pause()


# ---------------------------------------------------------------------------
# Section 7 — Domain Showcase: Customer Engagement
# ---------------------------------------------------------------------------

def section_7_customer_agent() -> None:
    header("SECTION 7 — Domain Showcase: Customer Engagement", "green")

    box(
        "Customer Engagement Specialist — Tool Stack",
        "Algolia NeuralSearch  → product catalog search (partner, primary)\n"
        "Amazon Personalize    → behavioral recommendations (AWS-native)\n"
        "Amazon Lex V2         → intent classification (AWS-native)\n"
        "Amazon Connect        → human escalation routing (AWS-native)\n\n"
        "Guardrails: NAME, EMAIL, PHONE, CREDIT_CARD → ANONYMIZED\n"
        "           competitor_pricing, internal_roadmap → BLOCKED",
    )

    from module5.agent import create_domain_agent

    print("\n  Creating customer domain agent...\n")
    agent = create_domain_agent("customer")

    # Query uses words that appear in product descriptions for reliable results
    q = (
        "We're having production incidents at 3am and need something with "
        "anomaly detection and automated incident response. What do you have?"
    )
    user_says(q)
    print()

    try:
        result = agent.agent.invoke({"messages": [("user", q)]})
        messages = result.get("messages", [])
        response = messages[-1].content if messages else "(no response)"
        print(f"\n  AGENT › {response}\n")
    except Exception as exc:
        print(
            f"\n  AGENT › [mock mode] I searched our catalog for incident alerting tools "
            f"and found three strong matches: DevOps Companion Pro (AI-powered anomaly "
            f"detection with automated incident response), Incident Response Playbook Suite "
            f"(automated escalation workflows with PagerDuty integration), and CloudWatch "
            f"Insights Accelerator. Based on your on-call use case, I'd recommend starting "
            f"with DevOps Companion Pro. Shall I pull personalized recommendations based "
            f"on your account history?\n"
            f"  (Live Bedrock call skipped: {exc})\n"
        )

    concept(
        "Same query, same model as the generic agent in Section 1 — "
        "completely different behavior because of the DomainConfig."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 8 — Domain Showcase: Geospatial Intelligence
# ---------------------------------------------------------------------------

def section_8_geospatial_agent() -> None:
    header("SECTION 8 — Domain Showcase: Geospatial Intelligence", "green")

    box(
        "Geospatial Intelligence Specialist — Two Provider Strategy",
        "Amazon Location Service (AWS-native, Esri data):\n"
        "  location_geocode, location_reverse_geocode,\n"
        "  location_calculate_route, location_search_nearby\n\n"
        "Mapbox (partner, global coverage + advanced analytics):\n"
        "  mapbox_forward_geocode, mapbox_get_directions,\n"
        "  mapbox_isochrone, mapbox_poi_search\n\n"
        "Guardrails: PII BLOCKED (not anonymized — location data is sensitive)\n"
        "Key constraint: NEVER estimate distances from memory — always use tools",
    )

    box(
        "Why Mapbox for Geospatial?",
        "Mapbox brings capabilities Amazon Location Service doesn't have:\n\n"
        "  • Isochrones — 'what area can I reach in 30 minutes?'\n"
        "    Generates a polygon of reachable area, not just a route.\n"
        "    Critical for deployment zone planning and field coverage analysis.\n\n"
        "  • POI Search — find specific place types near a coordinate\n"
        "    (data centers, co-location facilities, offices)\n\n"
        "  • Global routing with traffic-aware directions\n\n"
        "The agent uses both providers. AWS for core geocoding and routing,\n"
        "Mapbox for advanced spatial analytics.",
    )

    from module5.agent import create_domain_agent

    print("\n  Creating geospatial domain agent...\n")
    agent = create_domain_agent("geospatial")

    # Query that requires Location Service geocoding + Mapbox isochrone + Mapbox POI.
    # Uses real POI categories that Mapbox indexes (coffee shops, not data centers).
    q = (
        "Our field engineers are based in Seattle at 410 Terry Ave N. "
        "What area can they reach within 30 minutes by car, "
        "and what coffee shops or restaurants are within a 1km walk of that office "
        "for team meetups?"
    )
    user_says(q)
    print()

    try:
        result = agent.agent.invoke({"messages": [("user", q)]})
        messages = result.get("messages", [])
        response = messages[-1].content if messages else "(no response)"
        print(f"\n  AGENT › {response}\n")
    except Exception as exc:
        print(
            f"\n  AGENT › [mock mode] I geocoded 410 Terry Ave N to [-122.3367, 47.6205] "
            f"via Amazon Location Service, then called Mapbox Isochrone API to generate "
            f"the 30-minute reachable zone — roughly a 12km radius covering downtown Seattle, "
            f"SLU, Capitol Hill, and Eastlake. Mapbox POI search found 3 coffee shops and "
            f"2 restaurants within 1km of the office. "
            f"Shall I calculate drive times to specific destinations within the zone?\n"
            f"  (Live Bedrock call skipped: {exc})\n"
        )

    concept(
        "Mapbox isochrones answered a question Amazon Location Service can't: "
        "not 'how do I get there' but 'what can I reach from here.' "
        "That's the partner capability that makes this domain agent genuinely useful."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 9 — Domain Showcase: Voice Interaction
# ---------------------------------------------------------------------------

def section_9_voice_agent() -> None:
    header("SECTION 9 — Domain Showcase: Voice Interaction", "green")

    box(
        "Why Deepgram for Voice AI?",
        "Amazon Transcribe and Polly are solid batch processing tools.\n"
        "Deepgram is built for real-time, conversational voice pipelines.\n\n"
        "  Deepgram Nova-3 vs Amazon Transcribe:\n"
        "  • Latency:    <300ms streaming  vs  seconds (async job)\n"
        "  • Confidence: word-level scores  vs  overall job confidence\n"
        "  • Use case:   live voice agents  vs  batch file processing\n\n"
        "  Deepgram Aura-2 vs Amazon Polly:\n"
        "  • Prosody:    natural, conversational  vs  clear but robotic\n"
        "  • Latency:    streaming TTS            vs  full synthesis then play\n"
        "  • Use case:   voice agents, IVR        vs  narration, accessibility\n\n"
        "The agent uses Deepgram for real-time tasks, AWS for batch/fallback.\n"
        "The domain prompt and tool descriptions guide that decision.",
    )

    box(
        "Voice Interaction Specialist — Tool Stack",
        "Deepgram Nova-3    → real-time transcription (partner, primary)\n"
        "Deepgram Aura-2    → natural TTS synthesis   (partner, primary)\n"
        "Amazon Transcribe  → async batch transcription (AWS-native, fallback)\n"
        "Amazon Polly       → TTS with SSML support    (AWS-native, fallback)\n\n"
        "Guardrails: NAME, PHONE, EMAIL, SSN → ANONYMIZED\n"
        "           'password', 'secret key', 'access key' → WORD FILTERED\n"
        "Key constraint: NO markdown, NO bullet points, under 3 sentences",
    )

    # Step 1: Read local audio file and pass bytes directly to Deepgram — no S3 needed
    AUDIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo-support-call.mp3")

    print("\n  Step 1: Deepgram Nova-3 transcribes the support call...\n")
    transcript = None
    try:
        import json
        from deepgram import DeepgramClient

        with open(AUDIO_FILE, "rb") as f:
            audio_bytes = f.read()

        dg = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
        response = dg.listen.v1.media.transcribe_file(
            request=audio_bytes, model="nova-3", language="en-US",
        )
        results = response.results
        alt = results.channels[0].alternatives[0] if results.channels else None
        transcript = alt.transcript if alt else ""
        confidence = alt.confidence if alt else 0.0
        print(f"  DEEPGRAM TRANSCRIPT ({confidence*100:.0f}% confidence):")
        print(f"  \"{transcript}\"\n")
    except Exception as exc:
        transcript = (
            "Hey, this is Sarah from the platform team. Quick heads up — "
            "the notification service in our ECS cluster in us-east-1 has been "
            "restarting every 15 minutes since the last deployment. We're seeing "
            "out of memory errors in the logs and the health check is failing "
            "intermittently. Can someone take a look before the morning standup?"
        )
        print(f"  DEEPGRAM TRANSCRIPT (mock — {exc}):")
        print(f"  \"{transcript}\"\n")

    # Step 2: Pass the transcript we already have to the voice agent for summarization.
    # The agent doesn't re-transcribe — it receives the text and produces a
    # speech-optimized summary (no markdown, under 3 sentences).
    print("  Step 2: Voice agent produces spoken summary (no markdown, under 3 sentences)...\n")
    from module5.agent import create_domain_agent
    agent = create_domain_agent("voice", verbose=False)

    summary_text = None
    q = (
        f"Here is a support call transcript. Give me a spoken summary "
        f"I can relay to the on-call engineer:\n\n\"{transcript}\""
    )
    user_says("Summarize this support call transcript for the on-call engineer.")
    print()

    try:
        result = agent.agent.invoke({"messages": [("user", q)]})
        messages = result.get("messages", [])
        summary_text = messages[-1].content if messages else ""
        print(f"\n  AGENT › {summary_text}\n")
    except Exception as exc:
        summary_text = (
            "Sarah from the platform team reports the ECS notification service "
            "in us-east-1 has been restarting every 15 minutes since the last "
            "deployment with out-of-memory errors. Shall I page the on-call engineer?"
        )
        print(f"\n  AGENT › {summary_text}\n")
        print(f"  (Live Bedrock call skipped: {exc})\n")

    # Step 3: Synthesize the summary with Deepgram Aura-2 so the audience hears it
    print("  Step 3: Deepgram Aura-2 synthesizes the spoken summary...\n")
    try:
        import tempfile, subprocess
        from deepgram import DeepgramClient
        dg = DeepgramClient(api_key=os.environ["DEEPGRAM_API_KEY"])
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name
        chunks = list(dg.speak.v1.audio.generate(
            text=summary_text or "Summary unavailable.",
            model="aura-2-asteria-en",
        ))
        with open(tmp_path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        print(f"  DEEPGRAM AURA-2 OUTPUT: {tmp_path}")
        subprocess.Popen(["afplay", tmp_path])
        print("  ▶ Playing Deepgram Aura-2 synthesized summary...\n")
    except Exception as exc:
        print(f"  (Deepgram synthesis skipped: {exc})\n")

    concept(
        "Deepgram transcribed the audio in real time with word-level confidence. "
        "The agent summarized it in voice-optimized format — no markdown, under 3 sentences. "
        "Deepgram Aura-2 spoke it aloud. That's the full Deepgram voice pipeline end to end."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 10 — Domain Evaluation
# ---------------------------------------------------------------------------

def section_10_evaluation() -> None:
    header("SECTION 10 — Domain Evaluation: Quality Delta", "cyan")

    box(
        "Measuring the Value of Specialization",
        "How do we prove domain adaptation improves quality?\n\n"
        "Methodology:\n"
        "  1. Run the SAME test cases through a generic agent (no specialization)\n"
        "  2. Run the SAME test cases through the specialized domain agent\n"
        "  3. Score both on domain-specific criteria (0–100)\n"
        "  4. Quality delta = specialized_score − generic_score\n\n"
        "A positive delta proves the four levers are working.",
    )

    print("\n  Running module5 evaluation pipeline (mock mode)...\n")

    from evaluation.pipelines.module5_eval import run_module5_evaluation

    eval_results = run_module5_evaluation(domain="all", verbose=True)
    summary = eval_results["summary"]

    if _RICH:
        table = Table(title="Quality Delta by Domain", border_style="cyan", show_header=True)
        table.add_column("Domain", style="bold")
        table.add_column("Generic Score", style="red")
        table.add_column("Specialized Score", style="green")
        table.add_column("Quality Delta", style="bold yellow")
        table.add_column("Pass Rate", style="cyan")

        for domain_key, stats in summary.get("by_domain", {}).items():
            delta = stats["average_quality_delta"]
            delta_str = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"
            table.add_row(
                domain_key,
                f"{stats['average_generic_score']:.1f}/100",
                f"{stats['average_specialized_score']:.1f}/100",
                delta_str,
                f"{stats['pass_rate'] * 100:.1f}%",
            )

        # Overall row
        overall_delta = summary["average_quality_delta"]
        overall_delta_str = f"+{overall_delta:.1f}" if overall_delta >= 0 else f"{overall_delta:.1f}"
        table.add_row(
            "[bold]ALL DOMAINS[/bold]",
            f"{summary['average_generic_score']:.1f}/100",
            f"{summary['average_specialized_score']:.1f}/100",
            f"[bold]{overall_delta_str}[/bold]",
            f"{summary['pass_rate'] * 100:.1f}%",
        )
        _c.print(table)
    else:
        print(f"  Overall quality delta: +{summary['average_quality_delta']:.1f}")
        print(f"  Generic avg:           {summary['average_generic_score']:.1f}/100")
        print(f"  Specialized avg:       {summary['average_specialized_score']:.1f}/100")

    concept("Quality delta measures the value of specialization.")
    pause()


# ---------------------------------------------------------------------------
# Section 11 — Composing into Multi-Agent Architecture
# ---------------------------------------------------------------------------

def section_11_multi_agent_composition() -> None:
    header("SECTION 11 — Why This Approach vs Just Writing Separate Agents", "cyan")

    box(
        "The Alternative: Three Separate Hardcoded Agents",
        "You could write three separate agents from scratch:\n\n"
        "  customer_agent.py   — 200 lines, hardcoded prompt, hardcoded tools\n"
        "  geospatial_agent.py — 200 lines, hardcoded prompt, hardcoded tools\n"
        "  voice_agent.py      — 200 lines, hardcoded prompt, hardcoded tools\n\n"
        "Problems with that approach:\n"
        "  • Add a new domain → write another 200-line agent from scratch\n"
        "  • Change the base model → update every agent file\n"
        "  • Update a guardrail policy → find it buried in agent code\n"
        "  • Test quality → no standard evaluation interface\n"
        "  • Promote to production → no versionable artifact to diff\n\n"
        "The Domain Adaptation Engine solves all five.",
    )

    box(
        "What You Get Instead",
        "One engine. Declarative configs. Shared tool registry.\n\n"
        "  Add a new domain    → write a DomainConfig (30 lines)\n"
        "  Change base model   → update config/models.py once\n"
        "  Update guardrail    → edit GuardrailPolicy in the domain config\n"
        "  Test quality        → run_module5_evaluation() on any domain\n"
        "  Promote to prod     → diff two DomainConfig files like any code\n\n"
        "The DomainConfig is the versionable, promotable artifact.\n"
        "It's what your MLOps pipeline promotes through environments\n"
        "in Module 11 — not the agent code itself.",
    )

    code_block("""# The entire customer domain specialization is this config.
# This is what gets versioned, reviewed, tested, and promoted.

CUSTOMER_DOMAIN = DomainConfig(
    name="customer_engagement",
    prompt_layers=PromptLayers(role=..., knowledge_context=..., ...),
    tool_names=["algolia_semantic_search", "personalize_get_recommendations", ...],
    guardrail_policy=GuardrailPolicy(pii_handling="ANONYMIZE", ...),
    evaluation_criteria={"intent_accuracy": ..., "tone_appropriateness": ...},
)

# Plug into the Module 4 orchestrator as a callable tool:
customer_agent = create_domain_agent("customer")
result = customer_agent.agent.invoke({"messages": [("user", query)]})
""")

    concept(
        "Separate agents scale linearly with domains. "
        "The Domain Adaptation Engine scales with a config file. "
        "That's the difference between a project and a platform."
    )
    pause()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Module 5 Workshop Demo")
    parser.add_argument("--section", "-s", type=int, choices=range(1, 12), metavar="1-11")
    args = parser.parse_args()

    # Load .env file if present — search relative to this script's location first,
    # then fall back to the parent directory (agentic-ai/)
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _env_candidates = [
        os.path.join(_script_dir, "..", ".env"),          # agentic-ai/.env (normal)
        os.path.join(_script_dir, ".env"),                # demos/.env (fallback)
        os.path.join(os.getcwd(), ".env"),                # cwd/.env (last resort)
    ]
    for _env_file in _env_candidates:
        _env_file = os.path.normpath(_env_file)
        if os.path.exists(_env_file):
            with open(_env_file) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _v = _line.split("=", 1)
                        os.environ.setdefault(_k.strip(), _v.strip())
            break

    os.environ.setdefault("AGENT_MOCK_DOMAIN", "false")
    mock_on = os.environ.get("AGENT_MOCK_DOMAIN", "false").lower() == "true"
    if mock_on:
        print("  Mock mode ON  (domain tool data simulated, LLM calls are live via Bedrock)\n")
    else:
        print("  Live mode ON  (calling real domain tool endpoints)\n")

    header("MODULE 5 DEMO — DOMAIN-SPECIFIC AGENT APPLICATIONS", "bold cyan")
    print("""
  Use case: Building domain-specialized agents for customer engagement,
  geospatial intelligence, and voice interaction using a single
  Domain Adaptation Engine with four configurable levers.

  This demo covers:
    • Why generic agents fail on domain-specific queries
    • The Domain Adaptation Engine (DomainConfig + DomainAdapter)
    • Lever 1: Prompt engineering (PromptLayers)
    • Lever 2: Knowledge corpus grounding (Bedrock KB)
    • Lever 3: Tool scoping (ToolScoper as security boundary)
    • Lever 4: Guardrails (GuardrailPolicy via Bedrock Guardrails)
    • Live domain showcases: customer, geospatial, voice
    • Evaluation: quality delta generic vs specialized
    • Composing domain agents into the Module 4 orchestrator
""")
    pause("  ↵  Press Enter to begin...")

    sections = {
        1: section_1_specialization_problem,
        2: section_2_engine_anatomy,
        3: section_3_prompt_engineering,
        4: section_4_corpus_grounding,
        5: section_5_tool_scoping,
        6: section_6_guardrails,
        7: section_7_customer_agent,
        8: section_8_geospatial_agent,
        9: section_9_voice_agent,
        10: section_10_evaluation,
        11: section_11_multi_agent_composition,
    }

    if args.section:
        sections[args.section]()
    else:
        for fn in sections.values():
            fn()

    header("DEMO COMPLETE", "bold green")
    print("""
  ✅ You've seen:
     • Why generic agents fail on domain-specific queries
     • The Domain Adaptation Engine: DomainConfig + DomainAdapter
     • Four levers: prompt, corpus, tools, guardrails
     • Customer agent: Algolia + Personalize + Lex + Connect
     • Geospatial agent: Location Service + Mapbox (never estimates)
     • Voice agent: Deepgram + Transcribe + Polly (no markdown)
     • Quality delta evaluation: specialized vs generic scoring
     • Domain agents as building blocks for Module 4 orchestration

  Key Takeaways:
     • One engine, any domain — swap DomainConfig, get a new specialist
     • Tool scoping is a security boundary, not just a UX choice
     • Guardrails enforce policy at infrastructure level, not prompt level
     • Quality delta proves the value of specialization quantitatively

  Thank you for completing Module 5!
""")


if __name__ == "__main__":
    main()
