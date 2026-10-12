# Module 7: Agent Memory Systems

Module 7 of the **AI Agent Learning Series on AWS** (AWS Marketplace). Adds persistent memory to existing LangGraph agents using three ISV partners:

| Backend | Role | AWS Marketplace |
|---------|------|-----------------|
| **Redis Cloud** | Session memory — turn-by-turn conversation state, 24h TTL | [prodview-redis](https://aws.amazon.com/marketplace/search/results?searchTerms=redis+cloud) |
| **MongoDB Atlas** | Long-term semantic memory — episodic, procedural, consolidated | [prodview-mongodb](https://aws.amazon.com/marketplace/search/results?searchTerms=mongodb+atlas) |
| **Neo4j Aura** | Relationship memory — service dependencies, team ownership, blast radius | [prodview-xd42uzj2v7dae](https://aws.amazon.com/marketplace/pp/prodview-xd42uzj2v7dae) |

Memory is implemented as a **Domain Adaptation layer** using Module 5's `DomainAdapter` pattern — not a separate agent. Session continuity is handled at the framework level via a LangGraph checkpointer backed by Redis. Long-term semantic memory and relationship memory are exposed as six agent tools.

---

## Architecture

```
LangGraph ReAct Agent
    │
    ├── [Framework] Redis Cloud checkpointer
    │       └── Persists full message state between turns automatically
    │           Thread ID = session ID, 24h TTL
    │
    ├── [Tool] store_memory / recall_semantic_memory
    │       └── MongoDB Atlas Vector Search
    │           memory_type: episodic | procedural | consolidated
    │
    ├── [Tool] store_relationship / query_relationship_graph / get_blast_radius
    │       └── Neo4j Aura (Cypher, directed edges, multi-hop traversal)
    │
    └── [Tool] consolidate_session
            └── Bedrock LLM → extracts facts → MongoDB + Neo4j
```

---

## Usage Paths

### 1. Zero-Cost Mock Mode (no ISV accounts needed)

LLM calls via Bedrock are live. All memory operations use pre-built mock responses.

```bash
cd agentic-ai
source module7/.venv/bin/activate
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
AGENT_MOCK_MEMORY=true python demos/module7_demo.py
# Run a specific section:
AGENT_MOCK_MEMORY=true python demos/module7_demo.py --section 6
```

### 2. Free-Tier Live Mode

**Redis Cloud:** Sign up at [redis.io/cloud](https://redis.io/cloud) → create a free database → copy host, port, and password.

**MongoDB Atlas:** Sign up at [mongodb.com/atlas](https://mongodb.com/atlas) → create a free M0 cluster → create a Vector Search index (see below) → copy the connection URI.

**Neo4j:** Sign up at [neo4j.com/cloud](https://neo4j.com/cloud) → AuraDB Free → create instance → copy the connection URI and password.

```bash
export REDIS_HOST=your-host.db.redis.io
export REDIS_PORT=12345
export REDIS_USERNAME=default
export REDIS_PASSWORD=your-password

export MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net/?appName=App
export MONGODB_DATABASE=agent_memory
export MONGODB_COLLECTION=memories
export MONGODB_VECTOR_INDEX=vector_index

export NEO4J_URI=neo4j+s://xxxxx.databases.neo4j.io
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=your-password

python demos/module7_demo.py
```

### 3. AWS Marketplace Path (consolidated billing)

Subscribe to all three ISVs via AWS Marketplace for consolidated billing and enterprise support. Use the same environment variables as the free-tier path.

---

## MongoDB Atlas Vector Search Index

Before running in live mode, create a Vector Search index on the `agent_memory.memories` collection:

1. Atlas console → your cluster → **Atlas Search** → **Create Search Index**
2. Choose **Atlas Vector Search** → BYOE (Bring Your Own Embeddings)
3. Database: `agent_memory`, Collection: `memories`, Index Name: `vector_index`
4. Paste this definition:

```json
{
  "fields": [
    {"type": "vector", "path": "embedding", "numDimensions": 1024, "similarity": "cosine"},
    {"type": "filter", "path": "memory_type"},
    {"type": "filter", "path": "service_name"},
    {"type": "filter", "path": "severity"},
    {"type": "filter", "path": "agent_id"}
  ]
}
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENT_MOCK_MEMORY` | No | Set to `true` to use mock backends |
| `AWS_REGION` | Yes | AWS region for Bedrock (e.g., `us-east-1`) |
| `REDIS_HOST` | Live mode | Redis Cloud hostname |
| `REDIS_PORT` | Live mode | Redis Cloud port |
| `REDIS_USERNAME` | Live mode | Redis username (default: `default`) |
| `REDIS_PASSWORD` | Live mode | Redis password |
| `REDIS_SESSION_TTL` | No | Session TTL in seconds (default: 86400 = 24h) |
| `MONGODB_URI` | Live mode | MongoDB Atlas connection string |
| `MONGODB_DATABASE` | No | Database name (default: `agent_memory`) |
| `MONGODB_COLLECTION` | No | Collection name (default: `memories`) |
| `MONGODB_VECTOR_INDEX` | No | Vector index name (default: `vector_index`) |
| `NEO4J_URI` | Live mode | Neo4j Aura connection URI |
| `NEO4J_USERNAME` | Live mode | Neo4j username (default: `neo4j`) |
| `NEO4J_PASSWORD` | Live mode | Neo4j password |
| `MODULE7_INSECURE_TLS` | No | Local-dev only. `true` skips MongoDB/Neo4j TLS cert verification (macOS cert-chain workaround). Defaults to off (secure). Never enable in production. |
| `MODULE7_BEDROCK_GUARDRAIL_ID` | No | Production PII layer. When set, memory writes also pass through Amazon Bedrock Guardrails `ApplyGuardrail` for ML-based PII detection (e.g. names) on top of the always-on deterministic redaction. |
| `MODULE7_BEDROCK_GUARDRAIL_VERSION` | No | Guardrail version for the above (default: `DRAFT`). |

---

## Memory Governance (PII)

Every value written to a memory backend passes through a write-path guardrail
(`module7/memory/guardrails.py`) before it is persisted:

- **Always on (deterministic):** email addresses, phone numbers, and AWS access
  key IDs are redacted locally — no network call — in `store_memory` and during
  consolidation. This is enforced in both mock and live mode.
- **Production layer (opt-in):** set `MODULE7_BEDROCK_GUARDRAIL_ID` to additionally
  route content through **Amazon Bedrock Guardrails** (`ApplyGuardrail`) for
  ML-based PII detection such as person names. On any guardrail error the
  deterministic result is used, so a guardrail outage never blocks a write.

Raw personal data never lands in long-term storage.

## Production Integrations

The demo intentionally uses transparent, hand-written backend code so each
mechanism is visible (the `$vectorSearch` aggregation, the Redis checkpoint
keys). In production you can swap in the partners' officially supported
LangGraph/LangChain integrations:

- **Redis:** [`langgraph-checkpoint-redis`](https://github.com/redis-developer/langgraph-redis)
  (`RedisSaver` + `RedisStore`) — Redis-maintained checkpointer and store.
- **MongoDB Atlas:** [`langchain-mongodb`](https://github.com/langchain-ai/langchain-mongodb)
  (`MongoDBAtlasVectorSearch`) and `langgraph-checkpoint-mongodb` (`MongoDBSaver`) —
  vector store and checkpointer maintained under the LangChain–MongoDB partnership.

Both expose the same `BaseCheckpointSaver` / vector-store interfaces this module
targets, so they drop in without changing the agent architecture.

## Running Tests

```bash
cd agentic-ai
source module7/.venv/bin/activate
AGENT_MOCK_MEMORY=true pytest tests/test_module7_memory.py tests/test_module7_tools.py tests/test_module7_agent.py -v

# Also run the agent tests that invoke Amazon Bedrock (live, opt-in)
RUN_INTEGRATION_TESTS=true AGENT_MOCK_MEMORY=true pytest tests/test_module7_agent.py -v
```

The first command runs offline. Three agent tests
(`test_agent_invocation_returns_messages`, `test_session_continuity`,
`test_full_loop_references_memory_source`) call Bedrock, so they are skipped
unless `RUN_INTEGRATION_TESTS=true` is set. `module7/.venv` is created by
`./INSTALL.sh module7` from `agentic-ai/`.

---

## HTTP Server

```bash
AGENT_MOCK_MEMORY=true python -m module7.app
# Endpoints: GET /ping, GET /status, POST /invoke, POST /consolidate
# Default port: 8087 (override with MODULE7_PORT env var)
# Pass "session_id" in /invoke body for conversation continuity
```

---

## Seed & Verify

Seed the sample memory records and confirm all four connections:

```bash
python seed_live_memory.py            # seed episodic/procedural/graph (relative timestamps)
python verify_live_connections.py     # should show: 4x OK (Bedrock, Redis, MongoDB, Neo4j)
```

`seed_live_memory.py` is idempotent — memory document IDs are content hashes, so it
overwrites the sample records in place instead of creating duplicates. Re-run it any
time to reset the sample data to a current state.
