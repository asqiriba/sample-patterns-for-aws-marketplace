# Getting Started with the AI Agent Learning Series

This guide gets you from a fresh clone to a running demo for any module in the
series. The [README](README.md) has the full per-module quick start, the
environment matrix and the mock-flag reference.

## Prerequisites

- **Python 3.12** for Modules 1-9 (the tested version)
- **Python 3.13** for Module 10
- **Node.js 24** for Module 10 local mode
- **AWS CLI v2** and AWS credentials that can call Amazon Bedrock. Every demo
  that uses an LLM calls Bedrock, even in mock mode; Modules 6 and 8 and
  Module 10 `--mode mock` run without credentials.
- Git

Check the tools:

```bash
python3.12 --version
python3.13 --version   # Module 10 only
node --version         # Module 10 local mode only
aws --version
```

## Get the code

```bash
git clone https://github.com/aws-samples/sample-patterns-for-aws-marketplace.git
cd sample-patterns-for-aws-marketplace/agentic-ai
```

Run every command in this guide from `agentic-ai/`.

## Install

Modules install into four environment families, because they pin different
dependency sets. Start with the core family:

```bash
./INSTALL.sh                 # core: Modules 1-3, 5, 6, 8, 9, routing agent, evaluation, orchestration
source .venv/bin/activate
python verify_installation.py
```

The other families each get their own venv:

| Family | Install | Activate | Check |
| --- | --- | --- | --- |
| `module4` | `./INSTALL.sh module4` | `source module4/.venv/bin/activate` | `python verify_installation.py --family module4` |
| `module7` | `./INSTALL.sh module7` | `source module7/.venv/bin/activate` | `python verify_installation.py --family module7` |
| `module10` | `VENV_DIR=.venv-m10 ./INSTALL.sh module10` (or `./INSTALL.sh module10` if `.venv` is not the core env) | `source .venv-m10/bin/activate` | `python verify_installation.py --family module10` |

`INSTALL.sh` prints the next commands for the family you installed.

## Configure AWS

```bash
aws configure                       # or: aws login, or export AWS_PROFILE=<your-profile>
export AWS_REGION=us-east-1
aws sts get-caller-identity
```

Amazon Bedrock enables serverless models automatically on first invocation.
Anthropic models (the demos use Claude Sonnet 4.6) need a one-time use-case
submission per account: select an Anthropic model in the Bedrock console model
catalog. See [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).

## Run your first demo

One demo per family, all in mock mode (no partner accounts):

```bash
# core: Module 1, simulated AWS data, live Bedrock
source .venv/bin/activate
AGENT_MOCK_AWS=true python demos/module1_demo.py --section 4

# core: Module 6, fully offline
AGENT_MOCK_MODE=true python demos/module6_demo.py

# module4: the orchestrator calls a mocked Module 1
source module4/.venv/bin/activate
AGENT_MOCK_MODE=true python demos/module4_demo.py --section 3

# module7: memory backends mocked, live Bedrock
source module7/.venv/bin/activate
python demos/module7_demo.py --mock --no-pause

# module10: fully offline mock mode
source .venv-m10/bin/activate
python demos/module10_demo.py --mode mock --no-pause
```

Demos 1-6 and 8 pause between sections; press Enter to continue.

## Learning path

| Step | Module | What you learn |
| --- | --- | --- |
| 1 | [Module 1](module1/README.md) | Agent anatomy with AWS Strands: tools, the Think → Act → Observe loop, human-in-the-loop, model swap |
| 2 | [Module 2](module2/README.md) | The same patterns in LangChain and LangGraph, with LangSmith tracing |
| 3 | [Module 3](module3/README.md) | Evaluation (LLM-as-judge, Patronus AI, Deepchecks, Comet ML) and intent routing ([routing agent](routing_agent/README.md), [evaluation](evaluation/README.md)) |
| 4 | [Module 4](module4/README.md) | Multi-agent orchestration over HTTP and MCP |
| 5 | [Module 5](module5/README.md) | Domain-specific agents with the Domain Adaptation Engine |
| 6 | [Module 6](module6/README.md) | Durable workflows with Temporal |
| 7 | [Module 7](module7/README.md) | Agent memory with Redis Cloud, MongoDB Atlas and Neo4j Aura |
| 8 | [Module 8](module8/README.md) | Agent identity and access with Auth0 and CyberArk |
| 9 | [Module 9](module9/README.md) | Data pipelines and lineage with Confluent Cloud and Databricks |
| 10 | [Module 10](module10/README.md) | Agent exposure, capability rollout, model routing and trace inspection |

Each module stands on its own: run it in mock mode first, then follow its
README for live partner setup.

## Using Module 1 and Module 2 together (Python API)

```python
# Module 1: check AWS infrastructure (Strands agent)
from module1.agent import create_agent as create_infra_agent

infra_agent = create_infra_agent(verbose=True)
aws_status = infra_agent("Give me a health summary of us-east-1")
print(aws_status)

# Module 2: analyze a repository (LangGraph agent)
from module2.agent import analyze_repository

repo_analysis = analyze_repository("/path/to/repo", verbose=True)
print(repo_analysis["output"])
```

Module 4 coordinates these agents for you; see [Module 4](module4/README.md).

## LangSmith tracing (Modules 2 and 3)

LangChain and LangGraph send traces to LangSmith when these variables are set:

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=your-langsmith-api-key
export LANGCHAIN_PROJECT=repo-analysis-agent

# Run demo - traces appear in LangSmith dashboard
AGENT_MOCK_REPO=true python demos/module2_demo.py
```

View traces at: https://smith.langchain.com/

## Common issues

### `NoCredentialsError` or `Unable to locate credentials`

The demo called Amazon Bedrock without AWS credentials. Configure them
(`aws configure`, `aws login`, or `export AWS_PROFILE=<profile>`) and set
`AWS_REGION`. Mock flags such as `AGENT_MOCK_AWS=true` replace partner and AWS
resource data, not the LLM. To try something fully offline, run Module 6, 8 or
Module 10 `--mode mock`.

### `AccessDeniedException` when invoking Claude

Submit the Anthropic first-time use form once for the account (select an
Anthropic model in the Bedrock console model catalog), and check that your IAM
identity allows `bedrock:InvokeModel*` and the AWS Marketplace subscription
actions. Access can take a few minutes after the first invocation.

### `ImportError` for a package that is installed elsewhere

You are in the wrong environment family. Module 4 runs from `module4/.venv`,
Module 7 from `module7/.venv`, Module 10 from its Python 3.13 venv, and
everything else from the core `.venv`. `python verify_installation.py --family <family>`
shows what the active venv provides.

### `ModuleNotFoundError: No module named 'module1'` (or another module)

Run the command from `agentic-ai/`, not from inside a module directory. The
demos, tests and `python moduleN/app.py` entry points expect `agentic-ai/` as
the working directory.

## Resources

- [Series README](README.md) and [CHANGELOG](CHANGELOG.md)
- [Strands Agents documentation](https://strandsagents.com)
- [LangChain documentation](https://docs.langchain.com/oss/python/langchain/overview)
- [LangGraph documentation](https://docs.langchain.com/oss/python/langgraph/overview)
- [Amazon Bedrock](https://aws.amazon.com/bedrock/)
