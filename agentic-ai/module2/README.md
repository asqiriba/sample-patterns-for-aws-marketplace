# Module 2: Repository Analysis Agent

LangChain-based agent for analyzing git repositories to identify applications, technology stacks, and AWS infrastructure requirements.

## Overview

This module demonstrates the **Module 2 framework approach** using LangChain and LangGraph, complementing the Module 1 AWS Infrastructure Agent (Strands-based). Together, they form the foundation for multi-agent DevOps automation.

## What This Agent Does

1. **Scans** local git repositories to understand structure
2. **Detects** distinct applications/services (monorepo support)
3. **Analyzes** technology stacks (languages, frameworks, dependencies)
4. **Maps** dependencies to AWS infrastructure requirements
5. **Generates** structured analysis reports for deployment planning

## Quick Start

Run from `agentic-ai/` in the core environment (`./INSTALL.sh`, then
`source .venv/bin/activate`; see the [environment matrix](../README.md#environments)).
The agent calls Amazon Bedrock, so the demo needs AWS credentials even in mock mode.

```bash
# Mock mode (no real repository needed)
AGENT_MOCK_REPO=true python demos/module2_demo.py

# Analyze a real repository
python demos/module2_demo.py --repo /path/to/your/repo

# Run specific demo section
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 4

# Run tests
AGENT_MOCK_REPO=true pytest tests/test_repo_tools.py -v
```

## Architecture

### Framework: LangChain + LangGraph

**Model Interface**: `ChatBedrock` (LangChain wrapper for Amazon Bedrock)
**Execution Loop**: LangGraph `create_react_agent` (ReAct loop); `module2/workflows/analysis_graph.py` shows an explicit LangGraph state machine
**Tools**: 5 repository analysis tools
**Observability**: LangSmith tracing integration

### Five Repository Analysis Tools

1. **`scan_repository_structure`** - List files/directories with git awareness
2. **`read_file_content`** - Read specific files (package.json, requirements.txt, etc.)
3. **`detect_applications`** - Identify distinct apps/services in repository
4. **`analyze_dependencies`** - Parse dependency files and extract libraries
5. **`map_aws_services`** - Map dependencies to AWS services (RDS, ElastiCache, etc.)

## Framework Comparison: Module 1 vs Module 2

| Aspect | Module 1 (Strands) | Module 2 (LangChain) |
|--------|-------------------|---------------------|
| **Framework** | AWS Strands | LangChain + LangGraph |
| **Model Interface** | `BedrockModel` | `ChatBedrock` |
| **Agent Pattern** | `Agent` class | LangGraph `create_react_agent` |
| **Memory** | `SlidingWindowConversationManager` | LangGraph message state (no checkpointer configured) |
| **Observability** | Callback handlers | LangSmith tracing |
| **Use Case** | AWS infrastructure management | Repository analysis |
| **Tools** | AWS API calls (ECS, EC2, RDS) | Local git operations |

Both use the same Amazon Bedrock model and implement the think-act-observe loop.

## Example Output

```json
{
  "repository": "/path/to/repo",
  "applications": [
    {
      "name": "api-service",
      "path": "services/api",
      "stack": {
        "language": "Node.js",
        "runtime": "18.x",
        "framework": "Express",
        "dependencies": ["pg", "redis", "aws-sdk"]
      },
      "aws_requirements": {
        "compute": "ECS Fargate or Lambda",
        "database": "RDS PostgreSQL",
        "cache": "ElastiCache Redis",
        "storage": "S3",
        "networking": "VPC, ALB"
      }
    }
  ],
  "summary": {
    "total_applications": 1,
    "languages": ["Node.js"],
    "aws_services_needed": ["ECS", "RDS", "ElastiCache", "S3", "VPC", "ALB"]
  }
}
```

## Usage

### Python API

```python
from module2.agent import create_agent, analyze_repository

# Simple approach
agent = create_agent(verbose=True)
result = agent.invoke({"messages": [("user", "Analyze repository at /path/to/repo")]})
print(result["messages"][-1].content)

# Convenience function
results = analyze_repository("/path/to/repo")
print(results["output"])
```

### HTTP Server

```bash
# Start server
python module2/app.py

# Analyze repository
curl -X POST http://localhost:8081/analyze \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/path/to/repo"}'
```

## LangSmith Tracing

Enable LangSmith for detailed observability:

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=<your-key>
export LANGCHAIN_PROJECT=repo-analysis-agent

python demos/module2_demo.py
```

View traces at: https://smith.langchain.com/

## Dependency Mapping

The agent maps common libraries to AWS services:

| Dependency | AWS Service | Engine |
|------------|-------------|--------|
| `pg`, `psycopg2` | RDS | PostgreSQL |
| `mysql`, `mysql2` | RDS | MySQL |
| `mongodb`, `pymongo` | DocumentDB | MongoDB |
| `redis`, `ioredis` | ElastiCache | Redis |
| `boto3`, `aws-sdk` | S3 | Object Storage |
| `celery` | SQS | Message Queue |
| `express`, `fastapi` | ECS/Lambda | Web Framework |

See `module2/tools/repo_tools.py` for the complete mapping.

## Multi-Agent Integration

Module 2 is designed to work with Module 1 in a multi-agent workflow:

1. **Module 2 Agent** analyzes repository → identifies infrastructure needs
2. **Module 1 Agent** checks existing AWS resources → identifies gaps
3. **Orchestrator** coordinates both agents → generates deployment plan

This multi-agent pattern is implemented in Module 4 ([module4/README.md](../module4/README.md)).

## Project Structure

```
module2/
├── agent.py              # Agent factory and main logic
├── app.py                # HTTP server entrypoint
├── config/
│   └── models.py         # ChatBedrock configuration
├── tools/
│   └── repo_tools.py     # 5 repository analysis tools
├── workflows/
│   └── analysis_graph.py # LangGraph state machine
└── prompts/
    └── system_prompts.py # System prompts for each stage
```

## Testing

```bash
# Run all tests
AGENT_MOCK_REPO=true pytest tests/test_repo_tools.py -v

# Run specific test
AGENT_MOCK_REPO=true pytest tests/test_repo_tools.py::test_scan_repository_structure_mock -v

# Run the full analysis workflow test (mock data)
AGENT_MOCK_REPO=true pytest tests/test_repo_tools.py::test_full_analysis_workflow_mock -v
```

## Demo Sections

Run `AGENT_MOCK_REPO=true python demos/module2_demo.py --section N`:

| # | Title |
|---|-------|
| 1 | LangChain vs AWS Strands |
| 2 | Agent Identity & System Prompts |
| 3 | Context Management in LangChain |
| 4 | Chains: Composable Workflows |
| 5 | Tools in LangChain |
| 6 | Tools: LangChain vs Strands |
| 7 | Repository Structure Scan |
| 8 | Multi-Application Detection |
| 9 | Stack Analysis & AWS Service Mapping |
| 10 | LangSmith Tracing & Observability |
| 11 | Complete Analysis Pipeline |

## Next Steps

- **Module 3**: Evaluation and routing patterns
- **Module 4**: Multi-agent orchestration (Module 1 + Module 2 working together)
- **Module 7**: Agent memory with Redis Cloud, MongoDB Atlas and Neo4j Aura

## License

Part of the AI Agent Learning Series on AWS.
