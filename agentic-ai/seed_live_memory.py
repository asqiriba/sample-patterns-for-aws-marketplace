#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
seed_live_memory.py
===================
Seed representative infrastructure memory into the backends used by the
Module 7 walkthrough.

Populates the canonical sample records — a service incident, its resolution,
and the service dependency graph — across MongoDB Atlas and Neo4j. Timestamps
are relative to the current date, so the sample data stays current whenever
the script is run.

Idempotent: memory document IDs are derived from content, so re-running
overwrites the sample records in place rather than creating duplicates. Neo4j
writes use MERGE.

Usage:
    python seed_live_memory.py
    python verify_live_connections.py   # confirm 4x OK
"""
import os
import sys

# Load .env (same loader the demo uses) and force LIVE mode.
def _load_dotenv(env_path=None) -> None:
    """Load agentic-ai/.env if present; values already in the shell win."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
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
                if k:
                    os.environ.setdefault(k, v)


_load_dotenv()
os.environ.pop("AGENT_MOCK_MEMORY", None)  # never seed the mock backend

from module7.mock.mongo_mock import _ts  # relative-timestamp helper (single source)
from module7.tools.memory_tools import store_memory, store_relationship

# Representative sample records: two episodic observations for notification-svc
# (the incident and its resolution) and one procedural resolution pattern.
# Document IDs are content hashes, so these overwrite any existing copy in
# place rather than creating duplicates.
EPISODIC = [
    {
        "content": "notification-svc degraded: 0/3 ECS tasks running. Missing REDIS_URL environment variable.",
        "metadata": {"service_name": "notification-svc", "severity": "degraded",
                     "timestamp": _ts(1, 14, 32), "source_module": "module1"},
    },
    {
        "content": ("notification-svc outage resolved: injected REDIS_URL env var into ECS task definition "
                    "and forced new deployment. All 3/3 tasks healthy within 4 minutes."),
        "metadata": {"service_name": "notification-svc", "severity": "healthy",
                     "timestamp": _ts(1, 15, 45), "source_module": "module1"},
    },
]

PROCEDURAL = [
    {
        "content": ("notification-svc outage resolution: missing REDIS_URL env var. Fix: inject env var "
                    "into ECS task definition environment block, force new deployment. "
                    "Verified: 3/3 tasks healthy within 4 minutes."),
        "metadata": {"resolution_pattern": "inject REDIS_URL env var into ECS task definition and redeploy",
                     "service_name": "notification-svc", "timestamp": _ts(1, 15, 45)},
    },
]

RELATIONSHIPS = [
    ("notification-svc", "Service", "DEPENDS_ON", "api-gateway", "Service"),
    ("api-gateway", "Service", "DEPENDS_ON", "auth-svc", "Service"),
    ("platform-team", "Team", "OWNS", "api-gateway", "Service"),
    ("platform-team", "Team", "OWNS", "auth-svc", "Service"),
    ("notifications-team", "Team", "OWNS", "notification-svc", "Service"),
    ("api-gateway", "Service", "DEPLOYED_TO", "ecs-cluster-prod", "Infrastructure"),
]


def main() -> None:
    if os.getenv("AGENT_MOCK_MEMORY", "").lower() == "true":
        print("Refusing to seed in mock mode. Unset AGENT_MOCK_MEMORY.", file=sys.stderr)
        sys.exit(1)

    print("Seeding sample infrastructure memory (relative timestamps)...\n")

    print("MongoDB Atlas — episodic memory:")
    for rec in EPISODIC:
        store_memory.invoke({"content": rec["content"], "memory_type": "episodic",
                             "metadata": rec["metadata"]})
        print(f"  [{rec['metadata']['severity']:8}] {rec['metadata']['timestamp']}  "
              f"{rec['content'][:55]}")

    print("\nMongoDB Atlas — procedural memory:")
    for rec in PROCEDURAL:
        store_memory.invoke({"content": rec["content"], "memory_type": "procedural",
                             "metadata": rec["metadata"]})
        print(f"  {rec['metadata']['timestamp']}  {rec['content'][:55]}")

    print("\nNeo4j Aura — relationship graph:")
    for src, st, rel, tgt, tt in RELATIONSHIPS:
        store_relationship.invoke({"source": src, "source_type": st, "relationship": rel,
                                   "target": tgt, "target_type": tt})
        print(f"  ({src})-[{rel}]->({tgt})")

    print("\nDone. Run: python verify_live_connections.py")


if __name__ == "__main__":
    main()
