# Module 6: Durable Orchestration and Long-Running Workflows

Makes the DevOps Companion pipeline durable with **Temporal**: every agent step
becomes a checkpointed activity, so a crash, a throttled model call, or a
72-hour wait for human approval resumes the workflow instead of restarting it.

## What Module 6 teaches

- **Durable workflows**: the Module 2 → Module 3 → deploy pipeline as one Temporal workflow
- **Checkpoints**: each `execute_activity` result is recorded, so completed steps are not re-run
- **Crash and replay**: after a worker crash, replay skips completed activities and the workflow resumes
- **Signals**: a human approval gate that waits for a signal, with a timeout
- **Saga compensation**: rolling back completed deployment steps when a later step fails
- **Observability**: OpenTelemetry tracing across the workflow, with Datadog as the observability partner

## Two layers: teaching layer and real Temporal

| Package | What it is | Needs |
| --- | --- | --- |
| `module6/` | The teaching layer the demo and `module6/app.py` use. `create_workflow_orchestrator()` is always backed by the in-process `MockTemporalClient` (`module6/mock/temporal_mocks.py`), whatever `TEMPORAL_API_KEY` is set to. `module6/worker.py` shows the production worker configuration. | Nothing: runs offline |
| `orchestration/` | Real Temporal workflows (`DevOpsCompanionWorkflow`, `DeploymentSagaWorkflow`), activities that call the Module 2 and Module 3 HTTP servers, a worker and a client. | A Temporal server: Temporal Cloud, or a local dev server |

`orchestration/` reads `TEMPORAL_ENDPOINT`, `TEMPORAL_NAMESPACE`, `TEMPORAL_API_KEY`
and `TEMPORAL_TASK_QUEUE`. With no API key it connects to a dev server on
`localhost:7233` (namespace `default`) without TLS; with an API key it uses
Temporal Cloud with TLS.

```bash
# Real Temporal, local dev server (run `temporal server start-dev` first)
python -m orchestration.worker

# Real Temporal, Temporal Cloud
TEMPORAL_API_KEY=... TEMPORAL_ENDPOINT=<ns>.tmprl.cloud:7233 TEMPORAL_NAMESPACE=<ns> python -m orchestration.worker
```

The worker commands above are documented in `orchestration/worker.py`; they were
not run in the Module 11 maintenance release (no Temporal server or Temporal
Cloud account was used).

## Quick Start

Run from `agentic-ai/` in the core environment (`./INSTALL.sh`, then
`source .venv/bin/activate`; see the [environment matrix](../README.md#environments)).

```bash
# Full demo: fully offline, no AWS or Temporal credentials
AGENT_MOCK_MODE=true python demos/module6_demo.py

# Run a specific section (1-6)
AGENT_MOCK_MODE=true python demos/module6_demo.py --section 3
```

## Demo Sections

| # | Title | Key Concept |
|---|---|---|
| 1 | The Production Problem | Why durability matters |
| 2 | Durable Agent Activities | Activities as checkpoints, crash and replay |
| 3 | Parallel Verification (Fan-Out/Fan-In) | Independent checks run in parallel |
| 4 | Human-in-the-Loop for Approvals | Signals and approval timeouts |
| 5 | Advanced Durable Execution Patterns | Versioning and saga compensation |
| 6 | Observability: OpenTelemetry + Datadog | Tracing a workflow end to end |

## Tests

```bash
# Module 6 teaching layer (offline)
pytest tests/test_module6_tools.py -v

# Real Temporal workflows in orchestration/
pytest tests/test_orchestration.py -v
```

`tests/test_orchestration.py` uses `WorkflowEnvironment.start_local()`, which
downloads the Temporal dev server binary on first run, so it needs network
access (no Temporal account). Set `AGENT_NO_DELAY=true` to skip the simulated
0.5-1.5 s delays in the mock activities.

## HTTP Server

```bash
python module6/app.py
# Default: 127.0.0.1:8086 (override with MODULE6_HOST / MODULE6_PORT)
curl http://localhost:8086/ping
curl -X POST http://localhost:8086/start \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/app/nodejs-api", "region": "us-east-1"}'
```

| Method | Path | What it does |
| --- | --- | --- |
| GET | `/ping` | Health check |
| GET | `/status` | Current workflow status |
| GET | `/workers` | Worker pool and task queue information |
| POST | `/start` | Start a workflow (`repo_path`, `region`) |
| POST | `/signal` | Send the approval signal (`approved`, `reviewer`, `comments`) |
| POST | `/traces` | Return the workflow's trace spans |

The server has no authentication and binds to `127.0.0.1` by default; it is for
local demos only. POST bodies must be `application/json` (at most 1 MiB), and
when bound to loopback the `Host` header must be a loopback name. Binding a
non-loopback address prints a warning.

## Environment Variables

| Variable | Default | Used by |
| --- | --- | --- |
| `AGENT_MOCK_MODE` | `true` | Module 6 activities and demo (also read by Modules 4, 8 and 9; see the [mock flags](../README.md#mock-flags)) |
| `AGENT_NO_DELAY` | unset | Skips the simulated delays in the mock client and activities |
| `MODULE6_HOST` / `MODULE6_PORT` | `127.0.0.1` / `8086` | `module6/app.py` |
| `MODULE1_URL` / `MODULE2_URL` / `MODULE3_URL` | `http://localhost:8080` / `8081` / `8082` | Activities that call the earlier modules' servers |
| `TEMPORAL_ENDPOINT` / `TEMPORAL_NAMESPACE` / `TEMPORAL_API_KEY` / `TEMPORAL_TASK_QUEUE` | `localhost:7233` / `default` (`devops-companion` in `module6/config`) / unset / `devops-companion` | `orchestration/` (and `module6/worker.py`) |
| `DD_API_KEY` | unset | Datadog setting in `module6/config/models.py` (the demo shows Datadog output from mocks) |

## Project Structure

```
module6/
├── agent.py              # create_workflow_orchestrator() (MockTemporalClient)
├── app.py                # HTTP server (port 8086)
├── worker.py             # Production worker configuration pattern
├── activities/           # analysis, generation, verification, deployment, observability
├── config/
│   └── models.py         # Temporal, module URL and Datadog settings
├── mock/
│   ├── temporal_mocks.py # MockTemporalClient
│   └── aws_mocks.py      # Simulated AWS deployment results
└── workflows/
    └── devops_companion.py  # DevOpsCompanionWorkflow (teaching layer)
```

## Next Steps

- **Module 7**: agent memory (Redis Cloud, MongoDB Atlas, Neo4j Aura)

## License

Part of the AI Agent Learning Series on AWS.
