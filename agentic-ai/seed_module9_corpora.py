# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
seed_module9_corpora.py
========================
Idempotent seeder for the Module 9 corpora, mirroring seed_live_memory.py.

Warms the service and policy corpora and one historical deployment record
so the three-corpus story is complete and retrieval is non-empty before
the live demo. Safe to run repeatedly: content-derived doc ids overwrite
and the dedup index skips unchanged records.

Usage:
    # live (requires MONGODB_URI / NEO4J_* and AWS credentials for Titan)
    python seed_module9_corpora.py

    # offline rehearsal
    AGENT_MOCK_PIPELINE=true AGENT_MOCK_MEMORY=true python seed_module9_corpora.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv(env_path: str | os.PathLike | None = None) -> None:
    """Load agentic-ai/.env if present; values already in the shell win."""
    if env_path is None:
        root = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(root, ".env")
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
                if k and k not in os.environ:
                    os.environ[k] = v


def main() -> None:
    _load_dotenv()
    os.environ.setdefault("AGENT_MOCK_MODE", "true")

    from module9.ingestion.freshness import check_all_corpora
    from module9.ingestion.load import load_seed_corpora

    mock_memory = os.getenv("AGENT_MOCK_MEMORY", "").lower() == "true"
    print(f"Seeding Module 9 corpora "
          f"({'mock in-memory stores' if mock_memory else 'live Atlas + Aura'})...")

    results = load_seed_corpora()
    stored = [r for r in results if r.action == "stored"]
    skipped = [r for r in results if r.action != "stored"]
    for r in stored:
        print(f"  stored  {r.doc_id}  corpus={r.metadata.get('corpus')}")
    for r in skipped:
        print(f"  {r.action}  {r.doc_id}")

    print()
    for verdict in check_all_corpora():
        print(f"  {verdict['corpus']:<8} {verdict['status']:<6} "
              f"chunks={verdict['chunk_count']}")
    print("\nDone. Note: mock-memory seeding is in-process only; run the "
          "seeder without AGENT_MOCK_MEMORY to warm the live stores.")


if __name__ == "__main__":
    main()
