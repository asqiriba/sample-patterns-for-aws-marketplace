# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
demos/module7_demo.py
======================
Module 7: Agent Memory Systems — 9-section interactive demo.

Usage:
    python demos/module7_demo.py           # all sections, pauses between each
    python demos/module7_demo.py --section 6  # jump to one section
    python demos/module7_demo.py --no-pause   # run all without pausing (CI/review)

Sections:
    1. Why Agent Memory?
    2. Memory as Domain Adaptation (DAE pattern)
    3. Turn-by-Turn Session Memory (Redis)
    4. Long-Term Semantic Memory (MongoDB Atlas)
    5. Building the Relationship Graph (Neo4j)
    6. Blast Radius via Graph Traversal (Neo4j)
    7. Hybrid Memory Query (MongoDB + Neo4j)
    8. Memory Consolidation
    9. Full Memory-Augmented Agent Loop
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable

# Make the agentic-ai root importable for `python demos/module7_demo.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Auto-load .env from the agentic-ai directory if present
# ---------------------------------------------------------------------------

def _load_dotenv(env_path: str | os.PathLike | None = None) -> None:
    """Load .env file if present. WARNING: Never commit .env to version control.
    Ensure .env is listed in .gitignore before adding credentials.
    Values already set in the shell always win over .env.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if env_path is None:
        env_path = os.path.join(root, ".env")
    if os.path.exists(env_path):
        # Warn if .env is not in .gitignore
        gitignore_path = os.path.join(root, ".gitignore")
        if os.path.exists(gitignore_path):
            with open(gitignore_path) as gi:
                if ".env" not in gi.read():
                    print("⚠️  WARNING: .env exists but '.env' is not in .gitignore. "
                          "Do not commit credentials to version control.", file=sys.stderr)
        try:
            from dotenv import load_dotenv
        except ImportError:
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(env_path, override=False)
            return
        # Fallback parser: handles `export KEY=value` and quoted values.
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    if k and k not in os.environ:
                        os.environ[k] = v

_load_dotenv()

# Keep demo output readable: the MODULE7_INSECURE_TLS reminder is a dev-time
# log line (it stays active at WARNING for library/server usage); raise the
# threshold for these loggers so it isn't interleaved with the demo output.
import logging  # noqa: E402
for _logger_name in ("module7.memory.mongo_store", "module7.memory.neo4j_store"):
    logging.getLogger(_logger_name).setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Rich / plain-text output helpers  (mirrors module1/module5 pattern)
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text
    from rich import box as rich_box
    _c = Console()

    def header(n: int, title: str) -> None:
        _c.print()
        _c.print(Panel(
            f"[bold white]Section {n}[/bold white]  [cyan]{title}[/cyan]",
            style="bold cyan",
            border_style="cyan",
            padding=(0, 2),
        ))

    def concept(text: str) -> None:
        _c.print()
        _c.print(Panel(
            f"[yellow]{text}[/yellow]",
            title="[bold yellow]💡 Key Point[/bold yellow]",
            border_style="yellow",
            padding=(0, 1),
        ))
        _c.print()

    def user_says(text: str) -> None:
        _c.print(f"\n[bold bright_green]USER ›[/bold bright_green] [italic white]{text}[/italic white]")

    def box(title: str, body: str) -> None:
        _c.print(Panel(
            f"[dim]{body}[/dim]",
            title=f"[bold cyan]{title}[/bold cyan]",
            border_style="bright_black",
            padding=(0, 1),
        ))

    def show(label: str, data) -> None:
        """Display a label + value. Uses a Table for dicts, plain list for lists."""
        if isinstance(data, dict):
            t = Table(show_header=False, box=rich_box.SIMPLE, padding=(0, 1))
            t.add_column(style="green", no_wrap=True)
            t.add_column(style="white")
            for k, v in data.items():
                t.add_row(str(k), str(v))
            _c.print(f"[green]{label}[/green]")
            _c.print(t)
        elif isinstance(data, list):
            _c.print(f"[green]{label}:[/green]")
            for item in data:
                if isinstance(item, dict):
                    # Compact single-line dict display
                    parts = "  ".join(f"[dim]{k}[/dim] [white]{v}[/white]" for k, v in item.items())
                    _c.print(f"  • {parts}")
                else:
                    _c.print(f"  • [white]{item}[/white]")
        else:
            _c.print(f"[green]{label}:[/green] [white]{data}[/white]")

    def show_json(label: str, data) -> None:
        """Display raw JSON — use for data where structure matters (scores, nested objects)."""
        _c.print(f"[green]{label}:[/green]")
        _c.print_json(json.dumps(data, default=str))

    def info(text: str) -> None:
        _c.print(f"  [bright_black]{text}[/bright_black]")

    def think(text: str) -> None:
        _c.print(f"  [bold blue]🤔 THINK[/bold blue]  [blue]{text}[/blue]")

    def act(tool: str, args: str = "") -> None:
        _c.print(f"  [bold magenta]⚡ ACT[/bold magenta]   [magenta]{tool}[/magenta][dim]({args})[/dim]")

    def observe(tool: str, result: str) -> None:
        _c.print(f"  [bold green]👁  OBS[/bold green]    [dim]{tool} →[/dim] [white]{result}[/white]")

    def agent_says(text: str) -> None:
        from rich.markdown import Markdown
        _c.print()
        _c.print(Panel(
            Markdown(text),
            title="[bold cyan]AGENT[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        _c.print()

except ImportError:
    _c = None  # type: ignore[assignment]

    def header(n: int, title: str) -> None:
        print(f"\n{'='*62}\n  Section {n}: {title}\n{'='*62}")

    def concept(text: str) -> None:
        print(f"\n💡 Key Point: {text}\n")

    def user_says(text: str) -> None:
        print(f"\nUSER › {text}")

    def box(title: str, body: str) -> None:
        print(f"\n[ {title} ]\n{body}")

    def show(label: str, data) -> None:
        if isinstance(data, (dict, list)):
            print(f"{label}:\n{json.dumps(data, indent=2, default=str)}")
        else:
            print(f"{label}: {data}")

    def show_json(label: str, data) -> None:
        print(f"{label}:\n{json.dumps(data, indent=2, default=str)}")

    def info(text: str) -> None:
        print(f"  {text}")

    def think(text: str) -> None:
        print(f"  🤔 THINK  {text}")

    def act(tool: str, args: str = "") -> None:
        print(f"  ⚡ ACT    {tool}({args})")

    def observe(tool: str, result: str) -> None:
        print(f"  👁  OBS   {tool} → {result}")

    def agent_says(text: str) -> None:
        print(f"\nAGENT › {text}\n")


_NO_PAUSE = False  # set by --no-pause flag


def pause(msg: str = "  [dim]↵  Press Enter to continue...[/dim]") -> None:
    if _NO_PAUSE:
        return
    try:
        if _c is not None:
            _c.print(f"\n{msg}")
            input()
        else:
            input(f"\n  ↵  Press Enter to continue...")
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# Streaming Think → Act → Observe loop  (the key interactive pattern)
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    """Extract text from a streaming chunk's content field.

    langchain-aws 1.5.0 passes Bedrock streaming events through as-is:
    content = [{'type': 'text', 'text': 'Hello', 'index': 0}]
    or for tool use:
    content = [{'type': 'tool_use', 'partial_json': '...', 'index': 1}]
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def _extract_tool_use(content) -> list[dict]:
    """Extract tool_use blocks from a streaming chunk's content field."""
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict) and item.get("type") == "tool_use"]


def stream_agent_loop(agent, query: str, session_id: str | None = None) -> str:
    """
    Stream the LangGraph ReAct loop with true token-by-token output.

    Prints each phase as it arrives:
      🤔 THINK  — model reasoning before a tool call (streams token by token)
      ⚡ ACT    — tool name + args (printed when tool call is complete)
      👁  OBS   — tool result (printed immediately after tool returns)
      AGENT ›   — final answer (streams token by token)

    Returns the final response text.
    """
    from langchain_core.messages import ToolMessage

    user_says(query)
    print()

    # Build the LangGraph invocation config — thread_id enables checkpointer
    invoke_config = {}
    if session_id:
        invoke_config = {"configurable": {"thread_id": session_id}}

    final_response = ""
    in_think = False
    in_answer = False
    pending_tool_name = ""
    # Track partial JSON args per tool name for parallel tool calls
    pending_tool_args: dict[str, str] = {}

    for chunk, metadata in agent.stream(
        {"messages": [("user", query)]},
        stream_mode="messages",
        config=invoke_config if invoke_config else None,
    ):
        node = metadata.get("langgraph_node", "")
        content = chunk.content
        tool_calls = getattr(chunk, "tool_calls", []) or []

        # ── Tool result (OBSERVE) — arrives from the tools node ──────────
        if node == "tools" and isinstance(chunk, ToolMessage):
            if in_think:
                print()  # close the THINK line
                in_think = False
            result_preview = str(chunk.content)[:100].replace("\n", " ")
            if len(str(chunk.content)) > 100:
                result_preview += "..."
            observe(chunk.name or "tool", result_preview)
            print()
            continue

        if node != "agent":
            continue

        # ── Completed tool call decision (ACT) ───────────────────────────
        # tool_calls list is populated on the final chunk of a tool-use block
        if tool_calls:
            if in_think:
                print()
                in_think = False
            for tc in tool_calls:
                name = tc.get("name", "")
                if not name:
                    continue
                # Args aren't available in streaming mode (langchain-aws limitation).
                # Show a human-readable description based on the tool name instead.
                tool_descriptions = {
                    "recall_semantic_memory": "querying MongoDB Atlas for relevant memories",
                    "store_memory":           "storing observation in MongoDB Atlas",
                    "get_blast_radius":       "traversing Neo4j dependency graph",
                    "query_relationship_graph": "running Cypher query on Neo4j",
                    "store_relationship":     "writing relationship to Neo4j",
                    "consolidate_session":    "extracting durable facts via Bedrock",
                }
                description = tool_descriptions.get(name, "")
                act(name, description)
            continue

        # ── Streaming content tokens ──────────────────────────────────────
        text = _extract_text(content)
        tool_use_blocks = _extract_tool_use(content)

        if tool_use_blocks:
            for block in tool_use_blocks:
                name = block.get("name", "")
                partial = block.get("partial_json", "")
                if name:
                    if name != pending_tool_name:
                        pending_tool_name = name
                        if not in_think:
                            think(f"I need to call {name} to answer this")
                            in_think = True
                    # Accumulate args per tool name
                    if partial:
                        pending_tool_args[name] = pending_tool_args.get(name, "") + partial
            continue

        if not text:
            continue

        # Text token — could be pre-tool reasoning or final answer
        # Heuristic: if we've seen a tool call already, this is the final answer.
        # If not, it's the model thinking out loud before deciding on a tool.
        if not in_answer and not in_think:
            # First text token — start THINK display
            if _c is not None:
                _c.print("  [bold blue]🤔 THINK[/bold blue]  ", end="")
            else:
                print("  🤔 THINK  ", end="")
            in_think = True

        if in_think and pending_tool_name:
            # We were thinking and now getting text after a tool decision —
            # this is the final answer starting
            print()
            in_think = False
            in_answer = True
            if _c is not None:
                _c.print()  # blank line before streaming answer
            else:
                print("\nAGENT › ", end="")
        elif not in_answer and not in_think:
            in_answer = True
            if _c is not None:
                _c.print()
            else:
                print("\nAGENT › ", end="")

        if in_answer:
            # Stream plain text (strip markdown syntax) so it reads cleanly live
            plain = text.replace("**", "").replace("__", "").replace("`", "")
            print(plain, end="", flush=True)
            final_response += text  # accumulate original markdown for final render
        else:
            print(text, end="", flush=True)
    # Render the complete response as a markdown panel
    if final_response and _c is not None:
        from rich.markdown import Markdown
        _c.print(Panel(
            Markdown(final_response),
            title="[bold cyan]AGENT[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        _c.print()
    elif final_response:
        print(f"\nAGENT › {final_response}\n")

    return final_response


# ---------------------------------------------------------------------------
# Section 1: Why Agent Memory?
# ---------------------------------------------------------------------------

def demo_motivation() -> None:
    box(
        "The Problem",
        "A stateless agent is asked about a past incident it should know about.\n"
        "Watch it fail — then we'll fix it in Section 9.",
    )
    pause()

    from module7.config.models import get_chat_bedrock_model
    model = get_chat_bedrock_model()

    user_says("Is the notification-svc issue from yesterday resolved?")
    print()
    try:
        response = model.invoke([
            ("system", "You are an AWS infrastructure assistant. You have no memory of past events."),
            ("user", "Is the notification-svc issue from yesterday resolved?"),
        ])
        agent_says(response.content)
    except Exception as exc:
        agent_says(f"(Bedrock call failed: {exc})\nTip: run with AGENT_MOCK_MEMORY=true to use mock responses.")

    concept(
        "A stateless agent has no context about past events. Every conversation starts "
        "from zero — it cannot recall yesterday's incident, its root cause, or which "
        "team owns the affected service. Module 7 fixes this."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 2: Memory as Domain Adaptation
# ---------------------------------------------------------------------------

def demo_architecture() -> None:
    box(
        "The Architecture",
        "Memory is not a separate agent. It is a Domain Adaptation layer — "
        "the same pattern from Module 5 — composed onto any existing agent.\n\n"
        "Three ISV backends, three memory types:\n"
        "  Redis Cloud    → session memory  (turn-by-turn, 24h TTL)\n"
        "  MongoDB Atlas  → semantic memory (episodic, procedural, consolidated)\n"
        "  Neo4j Aura     → relationship memory (dependencies, ownership, blast radius)",
    )
    pause()

    from module7.config.memory_domain import MemoryDomainConfig

    config = MemoryDomainConfig("infrastructure")

    show("Domain name", config.name)
    show("Display name", config.display_name)
    show("6 memory tools added to the agent", config.tool_names)
    show("Memory backends", {
        "session (Redis)":    "turn-by-turn conversation history, 24h TTL",
        "semantic (MongoDB)": "episodic, procedural, consolidated — vector search",
        "graph (Neo4j)":      "service dependencies, team ownership, blast radius",
    })
    show("Neo4j node types", config.neo4j_node_types)
    show("PII guardrail (applied before storing)", {
        "pii_handling": config.guardrail_policy.pii_handling,
        "pii_entities": config.guardrail_policy.pii_entities,
    })
    pause("  ↵  Continue to DAE levers...")

    print()
    info("The 4 DAE levers applied to memory:")
    info("  1. System Prompt  → memory protocol: recall before answering, store after observing")
    info("  2. Knowledge Corpus → MongoDB Atlas (agent-written, not human-curated)")
    info("  3. Tool Scoping   → 6 memory tools added alongside the base agent's existing tools")
    info("  4. Guardrails     → PII anonymized before anything is stored")

    concept(
        "MemoryDomainConfig extends DomainConfig. DomainAdapter.adapt() does the rest. "
        "You can add memory to any agent from Modules 1–6 in one call — no rewriting."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 3: Turn-by-Turn Session Memory (Redis)
# ---------------------------------------------------------------------------

def demo_redis_session() -> None:
    box(
        "Session Memory — Redis Cloud",
        "Session continuity is handled at the framework level.\n"
        "A LangGraph checkpointer backed by Redis persists the full\n"
        "message state between turns — the agent never calls Redis directly.",
    )
    pause()

    from module7.agent import create_memory_agent

    info("Creating memory-augmented agent with Redis-backed checkpointer...")
    info("Pre-seeded: 3 episodic observations from prior Module 1 health checks.")
    agent, session_id = create_memory_agent(verbose=True)

    pause("  ↵  Turn 1: ask about notification-svc...")

    # Turn 1
    q1 = "Is the notification-svc issue from yesterday resolved?"
    user_says(q1)
    result1 = agent.invoke(
        {"messages": [("user", q1)]},
        config={"configurable": {"thread_id": session_id}},
    )
    msgs1 = result1.get("messages", [])
    agent_says(msgs1[-1].content if msgs1 else "(no response)")

    pause("  ↵  Turn 2: follow-up using 'it' — agent must remember the context...")

    # Turn 2 — vague reference, agent must use session context
    q2 = "What was the root cause of it?"
    user_says(q2)
    result2 = agent.invoke(
        {"messages": [("user", q2)]},
        config={"configurable": {"thread_id": session_id}},
    )
    msgs2 = result2.get("messages", [])
    agent_says(msgs2[-1].content if msgs2 else "(no response)")

    pause("  ↵  Now inspect Redis directly to prove the turns are persisted...")

    # Read the actual messages from the Redis checkpoint — show the conversation is stored
    info(f"\n[Diagnostic] Reading persisted conversation from Redis:")
    from module7.memory.redis_checkpointer import RedisCheckpointer
    import os

    cp = RedisCheckpointer()
    checkpoint_tuple = cp.get_tuple({"configurable": {"thread_id": session_id}})
    ttl = int(os.getenv("REDIS_SESSION_TTL", str(60 * 60 * 24)))

    if checkpoint_tuple:
        messages = checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])
        # Show only human and final AI messages (skip tool calls)
        visible = []
        for msg in messages:
            role = type(msg).__name__.replace("Message", "").lower()
            if role == "human":
                visible.append(("USER", str(msg.content)[:90]))
            elif role == "ai" and msg.content and not getattr(msg, "tool_calls", None):
                visible.append(("AGENT", str(msg.content)[:90]))

        info(f"  {len(messages)} messages stored in Redis  |  TTL: {ttl // 3600}h  |  Key: checkpoint:{session_id[:8]}...:latest")
        info("")
        for role, content in visible:
            prefix = "[bold bright_green]USER ›[/bold bright_green]" if role == "USER" else "[bold cyan]AGENT ›[/bold cyan]"
            if _c:
                _c.print(f"  {prefix} [dim]{content}[/dim]")
            else:
                info(f"  {role}: {content}")
    else:
        info(f"  Key: checkpoint:{session_id[:8]}...:latest")
        info(f"  TTL: {ttl // 3600}h")
        info("  (checkpoint stored in binary-encoded LangGraph format)")

    concept(
        "Session memory is framework infrastructure, not an agent tool. "
        "The LangGraph checkpointer backed by Redis persists the full message state "
        "after every turn — automatically, without the agent deciding to do it. "
        "The agent resolved 'it' in turn 2 because the prior messages were restored "
        "from Redis before the LLM was called. "
        "Redis Cloud on AWS Marketplace provides low single-digit millisecond reads and "
        "automatic TTL expiry — no cron jobs, no manual cleanup."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 4: Long-Term Semantic Memory (MongoDB Atlas)
# ---------------------------------------------------------------------------

def demo_mongo_semantic() -> None:
    box(
        "Long-Term Semantic Memory — MongoDB Atlas",
        "Observations are embedded by Bedrock Titan Embeddings v2 and stored\n"
        "in MongoDB Atlas. Vector Search finds semantically relevant memories\n"
        "across episodic, procedural, and consolidated memory types.",
    )
    pause()

    from module7.tools.memory_tools import store_memory, recall_semantic_memory
    from module7.mock.mongo_mock import _ts

    # Store a procedural resolution pattern — different from the degradation already in mock data
    observation = (
        "notification-svc outage resolved: injected REDIS_URL env var into ECS task definition "
        "and forced new deployment. All 3/3 tasks healthy within 4 minutes."
    )
    info(f"Storing episodic observation: {observation[:70]}...")
    print()

    result = store_memory.invoke({
        "content": observation,
        "memory_type": "episodic",
        "metadata": {
            "service_name": "notification-svc",
            "severity": "healthy",
            "timestamp": _ts(1, 15, 45),
            "source_module": "module1",
        },
    })
    show("MongoDB upsert result", result)

    pause("  ↵  Now query with semantic search...")

    query = "Have we seen container exit issues before?"
    info(f"\nQuery: '{query}'")
    print()

    info("Recall WITHOUT metadata filter:")
    results_all = json.loads(recall_semantic_memory.invoke({
        "query": query,
        "memory_type": "episodic",
        "top_k": 3,
    }))
    for i, r in enumerate(results_all, 1):
        sev = r["metadata"].get("severity", "?")
        sev_color = "green" if sev == "healthy" else "red" if sev == "degraded" else "yellow"
        if _c:
            _c.print(f"  [dim]#{i}[/dim]  [{sev_color}]{sev.upper()}[/{sev_color}]  "
                     f"[white]{r['content'][:75]}[/white]  [dim](score: {round(r['score'], 3)})[/dim]")
        else:
            info(f"  #{i}  {sev.upper()}  {r['content'][:75]}  (score: {round(r['score'], 3)})")
    info(f"  {len(results_all)} result(s) — relevance threshold: 0.4")

    pause("  ↵  Now add a severity=degraded filter...")

    info("\nRecall WITH filter: severity=degraded")
    results_filtered = json.loads(recall_semantic_memory.invoke({
        "query": query,
        "memory_type": "episodic",
        "filters": {"severity": "degraded"},
        "top_k": 3,
    }))
    if results_filtered:
        for i, r in enumerate(results_filtered, 1):
            if _c:
                _c.print(f"  [dim]#{i}[/dim]  [red]DEGRADED[/red]  "
                         f"[white]{r['content'][:75]}[/white]  [dim](score: {round(r['score'], 3)})[/dim]")
            else:
                info(f"  #{i}  DEGRADED  {r['content'][:75]}  (score: {round(r['score'], 3)})")
        removed = len(results_all) - len(results_filtered)
        info(f"  {len(results_filtered)} result(s) — filter removed {removed} non-degraded record(s)")
    else:
        info("  (no results matched the filter)")

    concept(
        "MongoDB Atlas Vector Search runs the $vectorSearch aggregation stage server-side — "
        "the filter is evaluated before the vector similarity search, not after. "
        "The document ID is a hash of the content, so storing the same observation twice "
        "overwrites the existing record. The agent writes and immediately queries — "
        "single-digit milliseconds from write to read, which is what agent memory requires."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 5: Building the Relationship Graph (Neo4j)
# ---------------------------------------------------------------------------

def demo_build_graph() -> None:
    box(
        "Relationship Memory",
        "After Module 2 analyzes the repo and Module 1 scans infrastructure, "
        "the agent populates a knowledge graph of service dependencies and ownership.",
    )
    pause()

    from module7.tools.memory_tools import store_relationship

    relationships = [
        ("notification-svc", "Service", "DEPENDS_ON", "api-gateway", "Service"),
        ("api-gateway",       "Service", "DEPENDS_ON", "auth-svc",    "Service"),
        ("platform-team",     "Team",    "OWNS",        "api-gateway", "Service"),
        ("platform-team",     "Team",    "OWNS",        "auth-svc",    "Service"),
        ("notifications-team","Team",    "OWNS",        "notification-svc", "Service"),
        ("api-gateway",       "Service", "DEPLOYED_TO", "ecs-cluster-prod", "Infrastructure"),
    ]

    info("Creating service dependency graph in Neo4j...")
    print()
    rel_symbols = {"DEPENDS_ON": "──▶", "OWNS": "──owns──▶", "DEPLOYED_TO": "──deployed──▶"}
    for src, src_type, rel, tgt, tgt_type in relationships:
        store_relationship.invoke({
            "source": src, "source_type": src_type,
            "relationship": rel,
            "target": tgt, "target_type": tgt_type,
        })
        sym = rel_symbols.get(rel, f"──{rel}──▶")
        if _c:
            _c.print(f"  [cyan]{src}[/cyan] [dim]{sym}[/dim] [cyan]{tgt}[/cyan]  [dim]({rel})[/dim]")
        else:
            info(f"  {src} {sym} {tgt}  ({rel})")

    concept(
        "Neo4j stores explicit relationships as first-class edges — not columns in a table, "
        "not metadata on a vector. MERGE semantics make every write idempotent. "
        "Cypher is a declarative graph query language that LLMs generate reliably, "
        "which matters when the agent needs to construct queries dynamically."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 6: Blast Radius via Graph Traversal
# ---------------------------------------------------------------------------

def demo_blast_radius() -> None:
    box(
        "Blast Radius Analysis",
        "notification-svc is degraded. Two Cypher queries answer the two\n"
        "questions every on-call engineer asks: what else is affected,\n"
        "and which teams do I page?",
    )
    pause()

    from module7.tools.memory_tools import get_blast_radius, query_relationship_graph

    info("Finding blast radius of notification-svc (2 hops)...")
    result_json = get_blast_radius.invoke({"service_name": "notification-svc", "hops": 2})
    result = json.loads(result_json)

    # Show as a dependency tree
    affected = result.get("affected_services", [])
    info("")
    info("  Blast radius of notification-svc (2 hops):")
    info("")
    info("    notification-svc")
    if "api-gateway" in affected:
        info("      └─ DEPENDS_ON → api-gateway       (hop 1)")
    if "auth-svc" in affected:
        info("           └─ DEPENDS_ON → auth-svc     (hop 2)")
    info("")
    show("Affected services", f"{len(affected)} service(s): {', '.join(affected)}")

    pause("  ↵  Now find team ownership...")

    info("Finding team ownership for affected services...")
    cypher = "MATCH (t:Team)-[:OWNS]->(s:Service) RETURN t.name AS team, s.name AS service ORDER BY t.name, s.name"
    ownership_raw = json.loads(query_relationship_graph.invoke({"cypher_query": cypher}))
    ownership_clean = []
    for r in ownership_raw:
        if "team" in r and "service" in r:
            ownership_clean.append({"team": r["team"], "service": r["service"]})
        elif "a" in r and "b" in r and r.get("r") == "OWNS":
            ownership_clean.append({"team": r["a"].get("name", "?"), "service": r["b"].get("name", "?")})
    if not ownership_clean:
        for r in ownership_raw:
            if r.get("r") == "OWNS":
                ownership_clean.append({
                    "team": r.get("a", {}).get("name", "?"),
                    "service": r.get("b", {}).get("name", "?"),
                })
    teams = sorted({r["team"] for r in ownership_clean})
    show("Team ownership", ownership_clean if ownership_clean else ownership_raw)
    show("Teams to page", ", ".join(teams) if teams else "(none found)")

    concept(
        "This is the question every on-call engineer asks: what else is affected, "
        "and who do I page? A two-hop graph traversal answers both in one query. "
        "Vector similarity cannot answer structural questions like this — "
        "it finds semantically similar content, not dependency chains."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 7: Hybrid Memory Query (MongoDB Atlas + Neo4j)
# ---------------------------------------------------------------------------

def demo_hybrid_query() -> None:
    box(
        "Hybrid Memory Query",
        "A question that requires both systems: Neo4j for structure,\n"
        "MongoDB Atlas for content.",
    )
    # NOTE: Tools are called directly here for demo predictability — the agent
    # would call these autonomously in production. Section 9 shows the agent
    # making these decisions live via the Think→Act→Observe loop.

    question = "What happened last time notification-svc went degraded, and what depends on it?"
    info(f"Question: '{question}'")
    pause("  ↵  Step 1: Neo4j — find services that notification-svc depends on...")

    from module7.tools.memory_tools import get_blast_radius, recall_semantic_memory

    blast_json = get_blast_radius.invoke({"service_name": "notification-svc", "hops": 2})
    blast = json.loads(blast_json)
    show("Blast radius of notification-svc (Neo4j)", {
        "affected_services": ", ".join(blast.get("affected_services", [])) or "(none)",
    })

    pause("  ↵  Step 2: MongoDB Atlas — recall past incidents...")

    memories = json.loads(recall_semantic_memory.invoke({
        "query": "service degraded incident ECS",
        "memory_type": "episodic",
        "top_k": 3,
    }))
    for i, m in enumerate(memories, 1):
        show_json(f"  Episodic memory {i}", {
            "score": round(m["score"], 3),
            "service": m["metadata"].get("service_name", "?"),
            "content": m["content"][:100],
            "when": m.get("when", ""),
        })

    pause("  ↵  Step 3: MongoDB Atlas — recall resolution patterns...")

    procedures = json.loads(recall_semantic_memory.invoke({
        "query": "how to resolve ECS service degradation missing env var",
        "memory_type": "procedural",
        "top_k": 2,
    }))
    for i, p in enumerate(procedures, 1):
        show(f"  Procedural memory {i}",
             p["metadata"].get("resolution_pattern", p["content"][:80]))

    concept(
        "Graph traversal and semantic search solve different problems. "
        "Neo4j answers structural questions: what depends on this, who owns it. "
        "MongoDB Atlas answers content questions: what happened, how was it resolved. "
        "Combining both in a single query chain gives the agent context that "
        "neither system could provide alone."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 8: Memory Consolidation
# ---------------------------------------------------------------------------

def demo_consolidation() -> None:
    box(
        "Memory Consolidation",
        "A 6-turn troubleshooting session is passed to the consolidation tool. "
        "Claude extracts durable facts and relationships, stores them in both backends.",
    )

    mock_session = [
        {"role": "user",      "content": "Why is notification-svc down?"},
        {"role": "assistant", "content": "notification-svc has 0/3 ECS tasks running."},
        {"role": "user",      "content": "How do we fix it?"},
        {"role": "assistant", "content": "The REDIS_URL env var is missing. Set it and redeploy."},
        {"role": "user",      "content": "Fixed! All 3 tasks are running now."},
        {"role": "assistant", "content": "notification-svc is healthy. Restarted ECS tasks after fixing REDIS_URL."},
    ]

    info(f"Session: {len(mock_session)} turns of troubleshooting notification-svc")
    for m in mock_session:
        info(f"  {m['role'].upper()}: {m['content']}")
    print()
    pause("  ↵  Run consolidation (live Bedrock call)...")

    from module7.tools.memory_tools import store_memory, _get_stores
    from module7.mock.mongo_mock import _ts
    from module7.memory.consolidation import ConsolidationService

    # NOTE: In production, the DomainAdapter PII guardrail anonymizes sensitive
    # data (names, emails, phone numbers, AWS keys) before it reaches the
    # consolidation model. The mock session here contains no PII by design.

    # Call the service directly to get the actual extracted facts and relationships
    _, neo4j, embeddings, _ = _get_stores()
    from module7.memory.mongo_store import MongoStore
    mongo = MongoStore()
    svc = ConsolidationService(mongo, neo4j, embeddings)
    consolidation_result = svc.consolidate(mock_session, "demo-agent-001")

    if _c:
        _c.print(f"\n  [green]Stored:[/green] [white]{consolidation_result.stored_count} items[/white]  "
                 f"[dim]({len(consolidation_result.facts)} facts + {len(consolidation_result.relationships)} relationships)[/dim]")
    else:
        info(f"  Stored: {consolidation_result.stored_count} items ({len(consolidation_result.facts)} facts + {len(consolidation_result.relationships)} relationships)")

    if consolidation_result.facts:
        info("")
        info("  Facts extracted → stored in MongoDB Atlas (consolidated):")
        for fact in consolidation_result.facts[:4]:
            if _c:
                _c.print(f"    [dim]•[/dim] [white]{fact[:90]}[/white]")
            else:
                info(f"    • {fact[:90]}")

    if consolidation_result.relationships:
        info("")
        info("  Relationships extracted → stored in Neo4j:")
        for rel in consolidation_result.relationships[:3]:
            subj = rel.get("subject", "?")
            pred = rel.get("predicate", "?")
            obj = rel.get("object", "?")
            if _c:
                _c.print(f"    [dim]•[/dim] [cyan]{subj}[/cyan] [dim]──{pred}──▶[/dim] [cyan]{obj}[/cyan]")
            else:
                info(f"    • ({subj})-[{pred}]->({obj})")

    # Also store a procedural resolution pattern so Section 9 can find it
    store_memory.invoke({
        "content": (
            "notification-svc outage resolution: missing REDIS_URL env var. "
            "Fix: inject env var into ECS task definition environment block, "
            "force new deployment. Verified: 3/3 tasks healthy within 4 minutes."
        ),
        "memory_type": "procedural",
        "metadata": {
            "service_name": "notification-svc",
            "resolution_pattern": "inject REDIS_URL env var into ECS task definition and redeploy",
            "timestamp": _ts(1, 15, 45),
            "source": "consolidation",
        },
    })

    concept(
        "Consolidation turns ephemeral chat into durable knowledge. "
        "Claude extracts facts (resolved issues, decisions, preferences) and "
        "relationships (service dependencies discovered during the session), "
        "stores facts in MongoDB Atlas consolidated memory type, relationships in Neo4j. "
        "Future sessions recall this without re-running the investigation."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 9: Full Memory-Augmented Agent Loop
# ---------------------------------------------------------------------------

def demo_full_loop() -> None:
    box(
        "Full Memory-Augmented Agent Loop",
        "Same question as Section 1. This time the agent has memory.\n"
        "Watch the Think → Act → Observe loop stream in real time.",
    )
    pause("  ↵  Create the memory-augmented agent...")

    # Use the factory — same call a customer would make.
    # streaming=True enables token-by-token output for the TAO loop.
    from module7.agent import create_memory_agent
    agent_graph, session_id = create_memory_agent(streaming=True, verbose=True)

    pause("  ↵  Ask the same question from Section 1...")

    final = stream_agent_loop(
        agent_graph,
        "Is the notification-svc issue from yesterday resolved, and are any dependent services still affected?",
        session_id=session_id,
    )

    # Verify the response references memory
    response_lower = final.lower()
    found = [t for t in ["episodic", "procedural", "graph", "memory", "notification-svc"]
             if t in response_lower]
    if found:
        info(f"✓ Response references: {found}")

    concept(
        "The agent recalled the degraded observation from episodic memory, "
        "pinpointed when it happened and the root cause, and gave actionable "
        "next steps — all without being told what happened. Compare this to "
        "Section 1. Same model. Same question. The only difference is the "
        "MemoryDomainConfig."
    )
    pause()


# ---------------------------------------------------------------------------
# Section registry and entry point
# ---------------------------------------------------------------------------

SECTIONS: dict[int, tuple[str, Callable[[], None]]] = {
    1: ("Why Agent Memory?",                              demo_motivation),
    2: ("Memory as Domain Adaptation (DAE Pattern)",      demo_architecture),
    3: ("Turn-by-Turn Session Memory (Redis Cloud)",      demo_redis_session),
    4: ("Long-Term Semantic Memory (MongoDB Atlas)",      demo_mongo_semantic),
    5: ("Building the Relationship Graph (Neo4j)",        demo_build_graph),
    6: ("Blast Radius via Graph Traversal (Neo4j)",       demo_blast_radius),
    7: ("Hybrid Memory Query (MongoDB + Neo4j)",          demo_hybrid_query),
    8: ("Memory Consolidation",                           demo_consolidation),
    9: ("Full Memory-Augmented Agent Loop",               demo_full_loop),
}


def main() -> None:
    global _NO_PAUSE

    parser = argparse.ArgumentParser(
        description="Module 7: Agent Memory Systems demo (9 sections)"
    )
    parser.add_argument("--section", type=int, metavar="N",
                        help="Run a specific section (1-9). Omit to run all.")
    parser.add_argument("--no-pause", action="store_true",
                        help="Skip all pause() prompts (useful for review/CI).")
    parser.add_argument("--mock", action="store_true",
                        help="Force mock mode (equivalent to AGENT_MOCK_MEMORY=true). "
                             "No ISV credentials required.")
    parser.add_argument("--live", action="store_true",
                        help="Force live mode. Requires REDIS_HOST, MONGODB_URI, "
                             "NEO4J_URI credentials in .env or environment.")
    args = parser.parse_args()

    if args.no_pause:
        _NO_PAUSE = True

    if args.mock:
        os.environ["AGENT_MOCK_MEMORY"] = "true"
    elif args.live:
        os.environ.pop("AGENT_MOCK_MEMORY", None)

    if args.section is not None:
        if args.section not in range(1, 10):
            print(f"Error: --section must be 1–9, got {args.section}", file=sys.stderr)
            sys.exit(1)
        title, fn = SECTIONS[args.section]
        header(args.section, title)
        fn()
    else:
        try:
            from rich.console import Console
            Console().print(Panel(
                "[bold white]Module 7: Agent Memory Systems[/bold white]\n\n"
                "[cyan]9 sections  ·  ~20 minutes  ·  press Enter to advance each beat[/cyan]\n\n"
                "  [bold]Redis Cloud[/bold]     [dim]→[/dim] session memory [dim](turn-by-turn, 24h TTL)[/dim]\n"
                "  [bold]MongoDB Atlas[/bold]   [dim]→[/dim] long-term semantic memory [dim](vector search)[/dim]\n"
                "  [bold]Neo4j AuraDB[/bold]    [dim]→[/dim] relationship memory [dim](graph traversal)[/dim]\n"
                "  [bold]Amazon Bedrock[/bold]  [dim]→[/dim] Claude Sonnet 4.6 [dim](reasoning)[/dim] + Titan Text Embeddings V2 [dim](vector generation)[/dim]\n\n"
                "[dim]Run a single section: python demos/module7_demo.py --section N[/dim]\n"
                "[dim]Mock mode (no ISV creds): python demos/module7_demo.py --mock[/dim]",
                border_style="cyan",
                padding=(1, 2),
            ))
        except ImportError:
            print("\n" + "="*62)
            print("  Module 7: Agent Memory Systems")
            print("  9 sections · ~20 minutes · press Enter to advance")
            print("="*62)

        pause("\n  ↵  Press Enter to begin...")

        for num, (title, fn) in SECTIONS.items():
            header(num, title)
            fn()
            print()

        try:
            from rich.console import Console
            Console().rule("[bold green]Demo Complete[/bold green]", style="green")
        except ImportError:
            print("\n" + "="*62 + "\n  Demo Complete\n" + "="*62)


if __name__ == "__main__":
    main()
