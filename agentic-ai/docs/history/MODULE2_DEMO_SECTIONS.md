> Historical development notes from the original module build. Kept for reference; they are not current instructions. See ../../README.md.

# Module 2 Demo - Section Guide

## Overview
The Module 2 demo now includes **8 comprehensive sections** that showcase the LangChain/LangGraph framework and repository analysis capabilities.

## Section Breakdown

### Section 1: Framework Comparison
**Focus**: Side-by-side comparison of Module 1 (Strands) vs Module 2 (LangChain)

**What you'll see**:
- Code examples showing both frameworks
- Same concepts, different APIs
- Model interfaces: `BedrockModel` vs `ChatBedrock`
- Agent patterns: `Agent` class vs `create_react_agent`
- Orchestration differences

**Key Takeaway**: Both frameworks implement the same think-act-observe loop, but with different levels of abstraction and flexibility.

---

### Section 2: Agent Identity & System Prompts ✨ NEW
**Focus**: How agents get their identity and capabilities

**What you'll see**:
- Complete `SYSTEM_PROMPT` structure from `module2/prompts/system_prompts.py`
- Agent role definition ("Repository Analysis Agent")
- Capability listing (5 tools)
- Workflow guidance (scan → detect → analyze → map → synthesize)
- Guidelines for behavior
- How the prompt is passed to `create_react_agent()`

**Key Takeaway**: System prompts are the agent's constitution - they define what the agent IS, what it CAN do, and HOW it should behave.

---

### Section 3: Context Management ✨ NEW
**Focus**: How conversation history is managed across frameworks

**What you'll see**:
- Module 1: `SlidingWindowConversationManager` (automatic, fixed window)
- Module 2: LangGraph message state (explicit, flexible)
- Message format: `{"messages": [("user", "query")]}`
- Full conversation history in state
- Multi-turn conversation examples
- Persistence and resumption capabilities

**Key Differences**:
- **Module 1**: Simple, automatic, good for single sessions
- **Module 2**: Explicit, flexible, supports persistence/branching

**Key Takeaway**: LangGraph's state-based approach makes context management explicit and enables advanced features like checkpointing and conversation branching.

---

### Section 4: Repository Structure Scan
**Focus**: Understanding the repository layout

**What you'll see**:
- Agent uses `scan_repository_structure` tool
- Identifies dependency files (package.json, requirements.txt)
- Finds configuration files (Dockerfile, Terraform)
- Git-aware file tree analysis
- Mock data showing multi-service repository

**Key Takeaway**: Unlike Module 1 which queries AWS APIs, Module 2 analyzes local git repositories to understand application structure.

---

### Section 5: Multi-Application Detection
**Focus**: Identifying distinct applications in monorepos

**What you'll see**:
- Agent uses `detect_applications` tool
- Finds multiple services in the repository
- Identifies each by dependency files and Dockerfiles
- Monorepo vs single-app detection
- Microservices architecture recognition

**Key Takeaway**: Modern repositories often contain multiple services. The agent identifies each service separately for individual stack analysis.

---

### Section 6: Stack Analysis & AWS Service Mapping
**Focus**: From dependencies to infrastructure requirements

**What you'll see**:
- Agent uses `analyze_dependencies` and `map_aws_services` tools
- Reads package.json, requirements.txt
- Maps libraries to AWS services:
  - `pg` → RDS PostgreSQL
  - `redis` → ElastiCache Redis
  - `boto3` → S3
  - `express` → ECS/Lambda
- Complete infrastructure requirements list

**Key Takeaway**: This is where repository analysis becomes infrastructure planning. The agent translates code dependencies into AWS service requirements.

---

### Section 7: LangSmith Tracing & Observability
**Focus**: Understanding agent behavior through tracing

**What you'll see**:
- LangSmith integration setup
- Environment variables configuration
- Trace visualization (conceptual)
- Comparison with Module 1's callback handlers
- Persistent tracing vs ephemeral logging

**Key Takeaway**: Observability is critical for agent development. LangSmith provides persistent tracing, evaluation, and debugging capabilities.

---

### Section 8: Complete Analysis Pipeline
**Focus**: End-to-end repository analysis workflow

**What you'll see**:
- Full workflow execution: scan → detect → analyze → map → synthesize
- Multi-step reasoning
- Tool orchestration
- Comprehensive analysis report
- Structured JSON output
- Integration with Module 1 (future)

**Key Takeaway**: The agent decomposes complex requests into multiple tool calls, analyzes systematically, and synthesizes findings into actionable reports.

---

## Running the Demo

### All Sections
```bash
AGENT_MOCK_REPO=true python demos/module2_demo.py
```

### Individual Sections
```bash
# Section 1: Framework comparison
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 1

# Section 2: Agent identity & prompts
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 2

# Section 3: Context management
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 3

# Section 4: Repository scan
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 4

# Section 5: Application detection
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 5

# Section 6: Dependency analysis
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 6

# Section 7: LangSmith tracing
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 7

# Section 8: Full workflow
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 8
```

### With Real Repository
```bash
python demos/module2_demo.py --repo /path/to/your/repo
```

## Learning Path

**Recommended order for learning**:
1. **Section 1** - Understand framework differences
2. **Section 2** - Learn how agents get their identity
3. **Section 3** - Understand context management
4. **Sections 4-6** - See the agent in action with tools
5. **Section 7** - Learn about observability
6. **Section 8** - See the complete workflow

**For quick demo**: Run sections 1, 2, 4, and 8

**For deep dive**: Run all sections in order

## Key Concepts Demonstrated

1. **Framework Agnostic**: Same concepts (prompts, tools, context) across Strands and LangChain
2. **Agent Identity**: System prompts define agent behavior
3. **Context Management**: Different approaches to conversation history
4. **Tool Orchestration**: Multi-step reasoning with external tools
5. **Observability**: Tracing and debugging agent behavior
6. **Multi-Agent Future**: Module 2 + Module 1 = Complete DevOps pipeline

## What's Next

After completing the Module 2 demo, you'll be ready for:
- **Module 3**: Evaluation and routing patterns
- **Module 4**: Multi-agent orchestration (Module 1 + Module 2)
- **Module 7**: Long-term memory with DynamoDB
- **Module 10**: RAG with Bedrock Knowledge Bases
