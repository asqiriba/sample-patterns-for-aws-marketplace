#!/usr/bin/env python3
"""
demos/module2_demo.py
=====================
Live workshop demonstration for Module 2: Agent Frameworks and Building Blocks.

Demonstrates LangChain/LangGraph agent analyzing repositories to identify
applications, technology stacks, and AWS infrastructure requirements.

USAGE
-----
  # Recommended: mock mode (no real repository needed)
  AGENT_MOCK_REPO=true python demos/module2_demo.py

  # Analyze a real repository
  python demos/module2_demo.py --repo /path/to/repo

  # Run just one section
  AGENT_MOCK_REPO=true python demos/module2_demo.py --section 3

SECTIONS
--------
  1  Framework comparison   — LangChain vs Strands architecture
  2  Repository scan        — File structure analysis
  3  Application detection  — Multi-app/monorepo identification
  4  Dependency analysis    — Stack and AWS service mapping
  5  LangSmith tracing      — Observability and debugging
  6  Full workflow          — Complete analysis pipeline
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
    from rich.syntax import Syntax
    _c = Console()

    def header(text: str, color: str = "cyan") -> None:
        _c.rule(f"[bold {color}]{text}[/bold {color}]", style=color)

    def concept(text: str) -> None:
        _c.print(f"\n[bold yellow]💡 Module 2 Concept:[/bold yellow] [yellow]{text}[/yellow]")

    def user_says(text: str) -> None:
        _c.print(f"\n[bold green]USER ›[/bold green] [italic]{text}[/italic]")

    def box(title: str, body: str) -> None:
        _c.print(Panel(f"[dim]{body}[/dim]", title=f"[bold]{title}[/bold]", border_style="cyan"))

    def code_block(code: str, language: str = "python") -> None:
        syntax = Syntax(code, language, theme="monokai", line_numbers=False)
        _c.print(syntax)

except ImportError:
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


def pause(msg: str = "  ↵  Press Enter to continue...") -> None:
    try:
        input(msg)
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# Section 1 — Framework Comparison
# ---------------------------------------------------------------------------

def section_1_framework_comparison() -> None:
    header("SECTION 1 — LangChain vs AWS Strands", "cyan")
    box(
        "Framework Architecture Comparison",
        "Module 1 uses AWS Strands. Module 2 uses LangChain + LangGraph.\n"
        "Both implement the same think-act-observe loop with different approaches.",
    )

    print("\n  MODULE 1 (AWS Strands) — Infrastructure Agent")
    code_block("""
from strands import Agent
from strands.models import BedrockModel

model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-6", region_name="us-east-1")
agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=ALL_TOOLS,
    conversation_manager=SlidingWindowConversationManager(),
)
response = agent("List ECS services in us-east-1")
    """)

    print("\n  MODULE 2 (LangChain) — Repository Analysis Agent")
    code_block("""
from langchain_aws import ChatBedrock
from langgraph.prebuilt import create_react_agent

model = ChatBedrock(model_id="us.anthropic.claude-sonnet-4-6", region_name="us-east-1")
agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)
response = agent.invoke({"messages": [("user", "Analyze repository at /path")]})
    """)

    concept(
        "Both frameworks use the same Amazon Bedrock model and implement the same "
        "think-act-observe loop. The difference is in the orchestration layer: "
        "Strands provides a simpler API, while LangChain offers more flexibility "
        "and integration with the broader LangChain ecosystem (LangGraph, LangSmith)."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 2 — Agent Identity & System Prompts
# ---------------------------------------------------------------------------

def section_2_agent_identity() -> None:
    header("SECTION 2 — Agent Identity & System Prompts", "magenta")
    box(
        "Defining the agent's role and capabilities",
        "System prompts are the agent's constitution - they define what the agent IS, "
        "what it CAN do, and HOW it should reason and respond.",
    )

    concept(
        "The system prompt is critical for agent behavior. It provides the agent with "
        "its identity, capabilities, workflow, and guidelines. This is where we tell "
        "the agent it's a 'Repository Analysis Agent' specialized in analyzing git repos."
    )

    print("\n  Module 2 System Prompt Structure:\n")
    code_block("""
# module2/prompts/system_prompts.py

SYSTEM_PROMPT = \"\"\"You are a Repository Analysis Agent specialized in 
analyzing software repositories to identify applications, technology stacks, 
and AWS infrastructure requirements.

## Your Role in Module 2
You analyze local git repositories to help DevOps engineers understand:
1. What applications/services exist in the repository
2. What technology stacks they use (languages, frameworks, dependencies)
3. What AWS infrastructure services they require

## Your Capabilities
You have access to five tools for repository analysis:
- scan_repository_structure: Get the file tree and identify key files
- read_file_content: Read specific files (package.json, requirements.txt)
- detect_applications: Identify distinct applications in the repository
- analyze_dependencies: Parse dependency files and extract libraries
- map_aws_services: Map dependencies to required AWS services

## Analysis Workflow
Follow this systematic approach:
1. **Scan** - Start with scan_repository_structure
2. **Detect** - Use detect_applications to identify apps/services
3. **Analyze** - For each app, read and analyze dependency files
4. **Map** - Map dependencies to AWS infrastructure requirements
5. **Synthesize** - Produce a comprehensive analysis report

## Guidelines
- Always call tools to gather data; never guess repository contents
- Be thorough: analyze all detected applications
- Provide specific AWS service recommendations
- Consider networking, security, and scalability requirements
\"\"\"
    """)

    print("\n  How it's used in the agent:\n")
    code_block("""
# module2/agent.py

from module2.prompts.system_prompts import SYSTEM_PROMPT

def create_agent():
    model = get_chat_bedrock_model()
    
    # LangGraph's create_react_agent accepts the system prompt
    agent = create_react_agent(
        model,
        ALL_TOOLS,
        prompt=SYSTEM_PROMPT,  # ← Agent identity defined here
    )
    
    return agent
    """)

    concept(
        "Compare this to Module 1 (Strands) which uses the same pattern: "
        "a SYSTEM_PROMPT string passed to the Agent constructor. Both frameworks "
        "use system prompts to define agent identity, but LangChain also supports "
        "stage-specific prompts for different workflow nodes (SCAN_PROMPT, "
        "DETECTION_PROMPT, etc.) when using the full LangGraph state machine."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 3 — Context Management
# ---------------------------------------------------------------------------

def section_3_context_management() -> None:
    header("SECTION 3 — Context Management in LangChain", "cyan")
    box(
        "How conversation history is managed",
        "LangChain automatically manages conversation context through message "
        "history, allowing the agent to maintain context across multiple turns.",
    )

    concept(
        "Module 1 uses Strands' SlidingWindowConversationManager to keep the last N "
        "messages in context. Module 2 uses LangGraph's built-in message state "
        "management, which automatically tracks the conversation history."
    )

    print("\n  Module 1 (Strands) Context Management:\n")
    code_block("""
from strands.agent.conversation_manager import SlidingWindowConversationManager

agent = Agent(
    model=model,
    tools=tools,
    system_prompt=SYSTEM_PROMPT,
    conversation_manager=SlidingWindowConversationManager(window_size=10),
    #                                                      ^^^^^^^^^^^^
    #                                                      Keeps last 10 messages
)
    """)

    print("\n  Module 2 (LangGraph) Context Management:\n")
    code_block("""
# LangGraph automatically manages message history in the state

agent = create_react_agent(
    model,
    tools,
    prompt=SYSTEM_PROMPT,
)

# Each invocation maintains its own message history
result = agent.invoke({
    "messages": [
        ("user", "Scan repository at /mock/repo"),
    ]
})

# The result contains the full conversation history
messages = result["messages"]
# [
#   HumanMessage(content="Scan repository at /mock/repo"),
#   AIMessage(content="I'll scan the repository..."),
#   ToolMessage(content="...scan results..."),
#   AIMessage(content="Based on the scan, I found..."),
# ]
    """)

    print("\n  Multi-turn conversation example:\n")
    code_block("""
# First turn
result1 = agent.invoke({
    "messages": [("user", "Scan /mock/repo")]
})

# Second turn - pass previous messages for context
result2 = agent.invoke({
    "messages": result1["messages"] + [
        ("user", "Now analyze the dependencies")
    ]
})

# The agent remembers the previous scan and can reference it
    """)

    concept(
        "LangGraph's state-based approach makes context management explicit and "
        "flexible. You can persist messages to a database (checkpointer), resume "
        "conversations, or branch conversations. Module 1's SlidingWindow is simpler "
        "but less flexible - it automatically manages a fixed-size window."
    )

    print("\n  Key Differences:\n")
    print("  Module 1 (Strands):")
    print("    • Automatic sliding window (last N messages)")
    print("    • Simple, opinionated approach")
    print("    • Good for single-session interactions\n")
    print("  Module 2 (LangGraph):")
    print("    • Explicit message state management")
    print("    • Full conversation history in state")
    print("    • Supports persistence, branching, resuming")
    print("    • Better for multi-session, complex workflows\n")

    pause()


# ---------------------------------------------------------------------------
# Section 4 — Chains: Composable Workflows
# ---------------------------------------------------------------------------

def section_4_chains_concept() -> None:
    header("SECTION 4 — Chains: Composable Workflows", "yellow")
    box(
        "Understanding LangChain's fundamental abstraction",
        "Chains are composable sequences of operations that transform inputs into outputs. "
        "They represent deterministic, predefined workflows.",
    )

    concept(
        "Chains vs Agents: Chains follow a fixed sequence (prompt → model → parser), "
        "while Agents dynamically decide which tools to use. Chains are predictable and "
        "efficient for known workflows. Agents are flexible for complex, unpredictable tasks."
    )

    print("\n  Simple Chain Pattern (LCEL - LangChain Expression Language):\n")
    code_block("""
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

# Step 1: Define prompt template
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a repository analysis expert."),
    ("human", "Analyze this repository: {repo_path}")
])

# Step 2: Get the model
model = ChatBedrock(model_id="us.anthropic.claude-sonnet-4-6")

# Step 3: Define output parser
output_parser = StrOutputParser()

# Step 4: Compose the chain using | operator
chain = prompt | model | output_parser

# Step 5: Invoke the chain
result = chain.invoke({"repo_path": "/path/to/repo"})
    """)

    concept(
        "The | (pipe) operator is LangChain's way of composing operations. "
        "Each component transforms data and passes it to the next. This is called "
        "LCEL (LangChain Expression Language) - a declarative way to build chains."
    )

    print("\n  Module 2 implements three types of chains:\n")
    print("  1. **Simple Chain**: prompt → model → output")
    print("     - Single-step analysis")
    print("     - Fast and predictable\n")
    print("  2. **Multi-Step Chain**: chain1 → chain2 → chain3")
    print("     - Sequential processing")
    print("     - Each step builds on previous results\n")
    print("  3. **Parallel Chain**: [chain1, chain2, chain3] → combine")
    print("     - Concurrent analysis branches")
    print("     - Results merged into final output\n")

    print("\n  Let's see a simple chain in action:\n")
    code_block("""
# module2/chains/analysis_chain.py

from module2.chains import create_simple_analysis_chain

# Create the chain
chain = create_simple_analysis_chain()

# Invoke with repository data
result = chain.invoke({
    "repo_path": "/mock/repo",
    "file_list": "package.json, Dockerfile, src/index.js, requirements.txt"
})

# Chain executes: prompt → model → parser → result
print(result)  # "This appears to be a Node.js application with..."
    """)

    print("\n  Running the simple chain with mock data:\n")
    
    # Actually run the chain
    try:
        from module2.chains import create_simple_analysis_chain
        
        chain = create_simple_analysis_chain()
        result = chain.invoke({
            "repo_path": "/mock/repo",
            "file_list": "services/api/package.json, services/api/Dockerfile, services/worker/requirements.txt, services/worker/Dockerfile, infrastructure/main.tf"
        })
        
        print(f"  CHAIN OUTPUT › {result}\n")
    except Exception as e:
        print(f"  [Note: Chain requires AWS credentials to run. Error: {type(e).__name__}]\n")

    concept(
        "Chains are deterministic and efficient. They're perfect for known workflows "
        "like 'analyze dependencies → map to AWS services → generate report'. "
        "Agents (which we'll use next) are better when you need dynamic tool selection."
    )

    print("\n  When to use Chains vs Agents:\n")
    print("  Use Chains when:")
    print("    • Workflow is known and fixed")
    print("    • Speed and cost are priorities")
    print("    • No tool selection needed\n")
    print("  Use Agents when:")
    print("    • Workflow depends on data")
    print("    • Need dynamic tool selection")
    print("    • Complex multi-step reasoning required\n")

    concept(
        "Module 2 uses BOTH: Chains for structured analysis steps, and Agents "
        "for dynamic repository exploration. This hybrid approach combines the "
        "efficiency of chains with the flexibility of agents."
    )

    pause()


# ---------------------------------------------------------------------------
# Section 5 — Tools in LangChain
# ---------------------------------------------------------------------------

def section_5_tools_in_langchain() -> None:
    header("SECTION 5 — Tools in LangChain", "blue")
    box(
        "How tools extend agent capabilities",
        "Tools are functions that agents can call to interact with external systems. "
        "In LangChain, tools are defined using the @tool decorator or Tool class.",
    )

    concept(
        "Tools are the agent's hands - they allow the agent to take actions in the world. "
        "Without tools, an agent can only think and respond. With tools, it can read files, "
        "query databases, call APIs, and perform any programmatic action."
    )

    print("\n  Defining a LangChain Tool (Method 1: @tool decorator):\n")
    code_block("""
from langchain_core.tools import tool

@tool
def scan_repository_structure(repo_path: str) -> str:
    \"\"\"
    Scan a git repository and return its file structure.
    
    Use this to understand the repository layout, identify dependency files,
    and locate configuration files.
    
    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root directory.
        
    Returns
    -------
    str
        JSON with file tree, dependency files, and config files.
    \"\"\"
    # Implementation here
    file_tree = get_file_tree(repo_path)
    return json.dumps({"file_tree": file_tree, "tool": "scan_repository_structure"})
    """)

    concept(
        "The @tool decorator automatically converts a Python function into a LangChain tool. "
        "The docstring becomes the tool description that the LLM sees. This is CRITICAL - "
        "the LLM uses the docstring to decide when to call the tool."
    )

    print("\n  Defining a LangChain Tool (Method 2: StructuredTool):\n")
    code_block("""
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

class ReadFileInput(BaseModel):
    repo_path: str = Field(description="Absolute path to repository root")
    file_path: str = Field(description="Relative path to file within repo")

def read_file_impl(repo_path: str, file_path: str) -> str:
    # Implementation
    content = Path(repo_path, file_path).read_text()
    return json.dumps({"content": content})

read_file_tool = StructuredTool.from_function(
    func=read_file_impl,
    name="read_file_content",
    description="Read the content of a specific file in the repository",
    args_schema=ReadFileInput,
)
    """)

    concept(
        "StructuredTool gives you more control over tool definition, especially for "
        "complex input schemas. The Pydantic model defines the tool's parameters with "
        "descriptions that help the LLM understand how to use the tool."
    )

    print("\n  Module 2's Tool Collection:\n")
    code_block("""
# module2/tools/repo_tools.py

from langchain_core.tools import tool

@tool
def scan_repository_structure(repo_path: str) -> str:
    \"\"\"Scan git repository and return file structure.\"\"\"
    pass

@tool
def read_file_content(repo_path: str, file_path: str) -> str:
    \"\"\"Read the content of a specific file.\"\"\"
    pass

@tool
def detect_applications(repo_path: str, file_tree: str) -> str:
    \"\"\"Detect distinct applications or services.\"\"\"
    pass

@tool
def analyze_dependencies(repo_path: str, app_path: str, dependency_file: str) -> str:
    \"\"\"Parse dependency file and extract dependencies.\"\"\"
    pass

@tool
def map_aws_services(dependencies: str) -> str:
    \"\"\"Map dependencies to required AWS services.\"\"\"
    pass

# Export all tools
ALL_TOOLS = [
    scan_repository_structure,
    read_file_content,
    detect_applications,
    analyze_dependencies,
    map_aws_services,
]
    """)

    print("\n  Passing Tools to the Agent:\n")
    code_block("""
# module2/agent.py

from langgraph.prebuilt import create_react_agent
from module2.tools.repo_tools import ALL_TOOLS
from module2.prompts.system_prompts import SYSTEM_PROMPT

def create_agent():
    model = get_chat_bedrock_model()
    
    # Create agent with tools
    agent = create_react_agent(
        model,
        ALL_TOOLS,  # ← Tools passed here as a list
        prompt=SYSTEM_PROMPT,
    )
    
    return agent
    """)

    concept(
        "The agent receives the tool list and automatically learns how to use them. "
        "The LLM reads each tool's name, description, and parameters to decide which "
        "tool to call and with what arguments. This is called 'tool calling' or 'function calling'."
    )

    print("\n  How the Agent Uses Tools (ReAct Loop):\n")
    print("  1. **Think**: LLM decides which tool to call based on the task")
    print("  2. **Act**: Agent calls the tool with specific arguments")
    print("  3. **Observe**: Agent receives the tool's output")
    print("  4. **Repeat**: Agent thinks about the result and decides next action\n")

    print("  Example execution:\n")
    code_block("""
User: "Analyze the repository at /mock/repo"

Agent Think: I need to first scan the repository structure
Agent Act:   scan_repository_structure(repo_path="/mock/repo")
Agent Observe: {"file_tree": "...", "dependency_files": ["package.json"]}

Agent Think: I found a package.json, let me read it
Agent Act:   read_file_content(repo_path="/mock/repo", file_path="package.json")
Agent Observe: {"content": "{\\"dependencies\\": {\\"express\\": \\"^4.18.0\\"}}"}

Agent Think: Now I can analyze the dependencies
Agent Act:   analyze_dependencies(repo_path="/mock/repo", app_path=".", 
                                   dependency_file="package.json")
Agent Observe: {"language": "Node.js", "dependencies": ["express"]}

Agent Respond: "This is a Node.js application using Express framework..."
    """)

    concept(
        "This multi-step reasoning is what makes agents powerful. The agent autonomously "
        "decides which tools to use, in what order, based on the task and intermediate results."
    )

    pause()


# ---------------------------------------------------------------------------
# Section 6 — Tools: LangChain vs Strands
# ---------------------------------------------------------------------------

def section_6_tools_comparison() -> None:
    header("SECTION 6 — Tools: LangChain vs Strands", "magenta")
    box(
        "Comparing tool patterns across frameworks",
        "Both LangChain and Strands use tools to extend agent capabilities, "
        "but with different APIs and patterns.",
    )

    concept(
        "Tools are a universal concept in agent frameworks. The core idea is the same: "
        "give the agent functions it can call. The difference is in how tools are defined, "
        "registered, and invoked."
    )

    print("\n  Module 1 (Strands) Tool Definition:\n")
    code_block("""
# module1/tools/aws_tools.py

from strands.agent.tool import Tool

def list_aws_resources_impl(service: str, region: str = "us-east-1") -> dict:
    \"\"\"Implementation function.\"\"\"
    if service == "ecs":
        return {"clusters": ["prod-cluster", "dev-cluster"]}
    # ... more logic

# Create Strands Tool
list_aws_resources = Tool(
    name="list_aws_resources",
    description="List AWS resources for a specific service (ECS, EC2, RDS, Lambda)",
    func=list_aws_resources_impl,
    parameters={
        "service": {
            "type": "string",
            "description": "AWS service name (ecs, ec2, rds, lambda)",
            "required": True,
        },
        "region": {
            "type": "string", 
            "description": "AWS region",
            "required": False,
        },
    },
)
    """)

    print("\n  Module 2 (LangChain) Tool Definition:\n")
    code_block("""
# module2/tools/repo_tools.py

from langchain_core.tools import tool

@tool
def scan_repository_structure(repo_path: str) -> str:
    \"\"\"
    Scan a git repository and return its file structure.
    
    Use this to understand the repository layout, identify dependency files,
    and locate configuration files.
    
    Parameters
    ----------
    repo_path : str
        Absolute path to the git repository root directory.
        
    Returns
    -------
    str
        JSON with file tree, dependency files, and config files.
    \"\"\"
    # Implementation
    file_tree = get_file_tree(repo_path)
    return json.dumps({"file_tree": file_tree})
    """)

    concept(
        "LangChain uses decorators (@tool) for a more Pythonic API. Strands uses explicit "
        "Tool objects with parameter schemas. Both approaches achieve the same goal: "
        "giving the LLM a structured description of what the tool does and how to use it."
    )

    print("\n  Key Differences:\n")
    print("  ┌─────────────────────┬──────────────────────────┬──────────────────────────┐")
    print("  │ Aspect              │ Strands (Module 1)       │ LangChain (Module 2)     │")
    print("  ├─────────────────────┼──────────────────────────┼──────────────────────────┤")
    print("  │ Definition          │ Tool() class             │ @tool decorator          │")
    print("  │ Parameters          │ Explicit schema dict     │ Function signature       │")
    print("  │ Description         │ description parameter    │ Docstring                │")
    print("  │ Return Type         │ Any Python object        │ String (JSON)            │")
    print("  │ Registration        │ Pass to Agent()          │ Pass to create_agent()   │")
    print("  │ Invocation          │ Automatic via Agent      │ Automatic via LangGraph  │")
    print("  └─────────────────────┴──────────────────────────┴──────────────────────────┘\n")

    print("\n  Module 1 (Strands) - Passing Tools to Agent:\n")
    code_block("""
from strands.agent import Agent
from module1.tools.aws_tools import ALL_TOOLS

agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=ALL_TOOLS,  # ← List of Tool objects
    conversation_manager=SlidingWindowConversationManager(),
)
    """)

    print("\n  Module 2 (LangChain) - Passing Tools to Agent:\n")
    code_block("""
from langgraph.prebuilt import create_react_agent
from module2.tools.repo_tools import ALL_TOOLS

agent = create_react_agent(
    model,
    ALL_TOOLS,  # ← List of @tool decorated functions
    prompt=SYSTEM_PROMPT,
)
    """)

    concept(
        "Both frameworks follow the same pattern: define tools, collect them in a list, "
        "pass them to the agent. The agent then uses the LLM to decide when and how to "
        "call each tool based on the task."
    )

    print("\n  Tool Output Format:\n")
    print("  Strands:")
    print("    • Returns Python objects (dicts, lists, etc.)")
    print("    • Framework handles serialization\n")
    print("  LangChain:")
    print("    • Returns strings (typically JSON)")
    print("    • Tool is responsible for formatting\n")

    print("\n  Example Tool Outputs:\n")
    code_block("""
# Strands Tool Output (Python dict)
{
    "clusters": ["prod-cluster", "dev-cluster"],
    "region": "us-east-1",
    "service": "ecs"
}

# LangChain Tool Output (JSON string)
'{
    "data": {
        "file_tree": "...",
        "dependency_files": ["package.json"]
    },
    "tool": "scan_repository_structure"
}'
    """)

    concept(
        "The key insight: tools are framework-agnostic at the concept level. Whether you "
        "use Strands or LangChain, you're giving the agent the same capability - the ability "
        "to call functions. The syntax differs, but the pattern is universal."
    )

    print("\n  Best Practices (Both Frameworks):\n")
    print("  1. **Clear descriptions**: LLM uses these to decide when to call the tool")
    print("  2. **Specific parameters**: Well-defined inputs help the LLM use tools correctly")
    print("  3. **Structured output**: Consistent format makes it easier for LLM to parse")
    print("  4. **Error handling**: Return error messages in the same format as success")
    print("  5. **Idempotency**: Tools should be safe to call multiple times\n")

    concept(
        "Module 1 uses 5 AWS tools (list, describe, health, summary, human review). "
        "Module 2 uses 5 repository tools (scan, read, detect, analyze, map). "
        "Different domains, same pattern: tools extend agent capabilities."
    )

    pause()


# ---------------------------------------------------------------------------
# Section 7 — Repository Scan
# ---------------------------------------------------------------------------

def section_7_repository_scan(agent) -> None:
    header("SECTION 7 — Repository Structure Scan", "green")
    box(
        "First step: Understanding the repository layout",
        "The agent uses scan_repository_structure to get an overview of files, "
        "directories, dependency files, and configuration files.",
    )

    concept(
        "Unlike Module 1 which queries AWS APIs, Module 2 analyzes local git repositories. "
        "The scan tool identifies dependency files (package.json, requirements.txt) "
        "and config files (Dockerfile, terraform files) to understand the stack."
    )

    q = "Scan the repository structure at /mock/repo and tell me what you find."
    user_says(q)
    print()
    response = agent.invoke({"messages": [("user", q)]})
    final_msg = response["messages"][-1].content
    print(f"\n  AGENT › {final_msg}\n")
    pause()


# ---------------------------------------------------------------------------
# Section 8 — Application Detection
# ---------------------------------------------------------------------------

def section_8_application_detection(agent) -> None:
    header("SECTION 8 — Multi-Application Detection", "blue")
    box(
        "Identifying distinct applications in a monorepo",
        "The agent detects separate applications by finding dependency files "
        "in different directories. This is crucial for monorepo analysis.",
    )

    concept(
        "Modern repositories often contain multiple services (microservices architecture). "
        "The agent identifies each service by its dependency file and Dockerfile, "
        "enabling separate stack analysis for each application."
    )

    q = "Detect all applications in the repository at /mock/repo. How many are there?"
    user_says(q)
    print()
    response = agent.invoke({"messages": [("user", q)]})
    final_msg = response["messages"][-1].content
    print(f"\n  AGENT › {final_msg}\n")
    pause()


# ---------------------------------------------------------------------------
# Section 9 — Dependency Analysis and AWS Mapping
# ---------------------------------------------------------------------------

def section_9_dependency_analysis(agent) -> None:
    header("SECTION 9 — Stack Analysis & AWS Service Mapping", "yellow")
    box(
        "From dependencies to AWS infrastructure requirements",
        "The agent reads dependency files, identifies libraries, and maps them "
        "to required AWS services (RDS, ElastiCache, S3, etc.).",
    )

    concept(
        "This is where repository analysis becomes infrastructure planning. "
        "The agent knows that 'pg' → RDS PostgreSQL, 'redis' → ElastiCache, "
        "'boto3' → S3/AWS SDK. It builds a complete infrastructure requirements list."
    )

    q = """For the repository at /mock/repo, for each application you detected:
1. Analyze its dependencies
2. Identify the technology stack (language, framework)
3. Map dependencies to AWS infrastructure services
4. Tell me what AWS services each application needs"""

    user_says(q)
    print()
    response = agent.invoke({"messages": [("user", q)]})
    final_msg = response["messages"][-1].content
    print(f"\n  AGENT › {final_msg}\n")
    pause()


# ---------------------------------------------------------------------------
# Section 10 — LangSmith Tracing
# ---------------------------------------------------------------------------

def section_10_langsmith_tracing() -> None:
    header("SECTION 10 — LangSmith Tracing & Observability", "magenta")
    box(
        "Observability: Understanding agent behavior",
        "LangSmith provides detailed traces of every agent run, showing each "
        "think step, tool call, and model response with timing data.",
    )

    print("""
  LangSmith Integration (when configured):
  
  1. Set environment variables:
     export LANGCHAIN_TRACING_V2=true
     export LANGCHAIN_API_KEY=<your-key>
     export LANGCHAIN_PROJECT=repo-analysis-agent
  
  2. Run the agent — traces automatically appear in LangSmith
  
  3. View in LangSmith dashboard:
     - Every model call with input/output
     - Every tool call with parameters and results
     - Token counts and latency for each step
     - Full conversation history
  
  4. Debug failed runs:
     - See exactly where the agent went wrong
     - Inspect the context window at each step
     - Identify tool errors or model confusion
  
  LangSmith is the Module 2 equivalent of Module 1's LoopObserver callback,
  but with persistent storage, search, and comparison across runs.
""")

    concept(
        "Observability is critical for agent development. Module 1 uses callback handlers "
        "to print loop steps. Module 2 uses LangSmith for persistent tracing, evaluation, "
        "and debugging. Both serve the same purpose: making agent behavior transparent."
    )
    pause()


# ---------------------------------------------------------------------------
# Section 11 — Full Analysis Workflow
# ---------------------------------------------------------------------------

def section_11_full_workflow(agent) -> None:
    header("SECTION 11 — Complete Analysis Pipeline", "red")
    box(
        "End-to-end repository analysis",
        "The agent executes the full workflow: scan → detect → analyze → map → synthesize",
    )

    concept(
        "This demonstrates the complete Module 2 capability: analyzing a repository "
        "and producing a structured report with all applications, their stacks, and "
        "AWS infrastructure requirements. This output can be consumed by Module 1 "
        "(the AWS Infrastructure Agent) to check existing resources and identify gaps."
    )

    q = """Perform a complete analysis of the repository at /mock/repo:

1. Scan the repository structure
2. Detect all applications/services
3. For each application:
   - Analyze its technology stack
   - Identify dependencies
   - Map to AWS infrastructure requirements
4. Produce a comprehensive report with:
   - Repository overview
   - Application details (stack, dependencies, AWS needs)
   - Infrastructure summary (all AWS services needed)
   - Deployment recommendations

Format the final report as structured JSON."""

    user_says(q)
    print()
    response = agent.invoke({"messages": [("user", q)]})
    final_msg = response["messages"][-1].content
    print(f"\n  AGENT › {final_msg}\n")
    
    concept(
        "The agent decomposed this complex request into multiple tool calls, "
        "analyzed the repository systematically, and synthesized findings into "
        "a comprehensive report. This is the multi-step reasoning capability "
        "that makes agents powerful for complex analysis tasks."
    )
    pause()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Module 2 Workshop Demo")
    parser.add_argument("--section", "-s", type=int, choices=range(1, 12), metavar="1-11")
    parser.add_argument("--repo", type=str, help="Path to real repository to analyze")
    args = parser.parse_args()

    # Set mock mode if no real repo provided
    if not args.repo:
        os.environ["AGENT_MOCK_REPO"] = "true"
        print("  Mock mode ON  (pass --repo /path to analyze a real repository)\n")

    from module2.agent import create_agent

    agent = create_agent(verbose=True, max_iterations=15)

    header("REPOSITORY ANALYSIS AGENT — MODULE 2 DEMO", "bold cyan")
    print("""
  Use case: DevOps Engineer analyzing a software repository to understand
  what applications exist, what technology stacks they use, and what AWS
  infrastructure services they require.

  Module 2 scope: REPOSITORY ANALYSIS
  - Scan git repository structure
  - Detect applications/services
  - Analyze technology stacks
  - Map dependencies to AWS services
  
  Framework: LangChain + LangGraph (vs Module 1's AWS Strands)
""")
    pause("  ↵  Press Enter to begin...")

    sections = {
        1: section_1_framework_comparison,
        2: section_2_agent_identity,
        3: section_3_context_management,
        4: section_4_chains_concept,
        5: section_5_tools_in_langchain,
        6: section_6_tools_comparison,
        7: lambda: section_7_repository_scan(agent),
        8: lambda: section_8_application_detection(agent),
        9: lambda: section_9_dependency_analysis(agent),
        10: section_10_langsmith_tracing,
        11: lambda: section_11_full_workflow(agent),
    }

    if args.section:
        sections[args.section]()
    else:
        for fn in sections.values():
            fn()

    header("DEMO COMPLETE", "bold green")
    print("""
  ✅ You've seen:
     • LangChain vs AWS Strands framework comparison
     • Agent identity and system prompts
     • Context management (message history)
     • Chains: composable workflows (LCEL)
     • Tools in LangChain: definition and usage
     • Tools comparison: LangChain vs Strands
     • Repository structure scanning with git awareness
     • Multi-application detection in monorepos
     • Dependency analysis and AWS service mapping
     • LangSmith tracing for observability
     • Complete analysis workflow with structured output

  🔜 Next: Module 3 — Evaluation and Routing Patterns
  
  💡 Multi-Agent Future:
     Module 2 Agent (repo analysis) + Module 1 Agent (AWS infrastructure)
     = Complete DevOps automation pipeline
""")


if __name__ == "__main__":
    main()
