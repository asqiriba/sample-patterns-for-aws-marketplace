#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
verify_installation.py
======================
Verify that the modules of one install family are importable and that the
demo scripts and documentation are present.

    python verify_installation.py [--family core|module4|module7|module10]

Each family matches an ``./INSTALL.sh <family>`` target and its virtual
environment. Modules outside the chosen family are reported as SKIP.

This script only imports modules. It does not create agents or call AWS, and
it forces every AGENT_MOCK_* flag to "true" before importing anything.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Imports only: make sure no module reaches for a live backend at import time.
for _flag in (
    "AGENT_MOCK_AWS",
    "AGENT_MOCK_REPO",
    "AGENT_MOCK_MODE",
    "AGENT_MOCK_DOMAIN",
    "AGENT_MOCK_MEMORY",
    "AGENT_MOCK_PIPELINE",
):
    os.environ[_flag] = "true"

# Add project root to path
sys.path.insert(0, str(ROOT))

# (label, [(module path, [symbols])]) per family.
FAMILIES: dict[str, list[tuple[str, list[tuple[str, list[str]]]]]] = {
    "core": [
        ("Module 1 (Strands infrastructure agent)", [
            ("module1.agent", ["create_agent"]),
            ("module1.tools.aws_tools", ["ALL_TOOLS"]),
        ]),
        ("Module 2 (LangChain repository agent)", [
            ("module2.agent", ["create_agent"]),
            ("module2.tools.repo_tools", ["ALL_TOOLS"]),
            ("module2.workflows.analysis_graph", ["create_analysis_graph"]),
        ]),
        ("Module 3 (CDK generation and evaluation)", [
            ("module3.agent", ["generate_infrastructure"]),
            ("module3.tools.cdk_tools", ["ALL_TOOLS"]),
        ]),
        ("Routing agent", [
            ("routing_agent.agent", ["classify_intent"]),
        ]),
        ("Evaluation harness", [
            ("evaluation.pipelines.module3_eval", []),
        ]),
        ("Module 5 (domain adaptation)", [
            ("module5.engine.domain_adapter", ["DomainAdapter"]),
            ("module5.tools", ["ALL_TOOLS"]),
        ]),
        ("Module 6 (durable workflows)", [
            ("module6.agent", ["create_workflow_orchestrator"]),
        ]),
        ("Orchestration (real Temporal)", [
            ("orchestration", ["is_available"]),
        ]),
        ("Module 8 (agent identity)", [
            ("module8.identity.delegation", ["authorize_agent_call"]),
        ]),
        ("Module 9 (data pipelines and lineage)", [
            ("module9.agent", ["create_pipeline_agent"]),
        ]),
    ],
    "module4": [
        ("Module 4 (multi-agent orchestration)", [
            ("module4.agent", ["create_orchestrator"]),
            ("module4.protocols", []),
        ]),
    ],
    "module7": [
        ("Module 7 (agent memory)", [
            ("module7.agent", ["create_memory_agent"]),
            ("module7.tools.memory_tools", ["MEMORY_TOOLS"]),
        ]),
    ],
    "module10": [
        ("Module 10 (agent exposure and integration)", [
            ("module10.runtime", ["build_runtime"]),
            ("module10.server", []),
        ]),
    ],
}

DEMOS = [f"demos/module{n}_demo.py" for n in range(1, 11)]

DOCS = [
    "README.md",
    "GETTING_STARTED.md",
    "CHANGELOG.md",
    "module1/README.md",
    "module2/README.md",
    "module3/README.md",
    "module4/README.md",
    "module5/README.md",
    "module6/README.md",
    "module7/README.md",
    "module8/README.md",
    "module9/README.md",
    "module10/README.md",
    "routing_agent/README.md",
    "evaluation/README.md",
    "docs/history/README.md",
]


def verify_imports(label: str, checks: list[tuple[str, list[str]]]) -> bool:
    """Import each module and look up its key symbols."""
    try:
        for module_path, symbols in checks:
            module = importlib.import_module(module_path)
            for symbol in symbols:
                getattr(module, symbol)
    except Exception as exc:  # report any import-time failure, keep going
        print(f"  ✗ FAIL  {label}: {type(exc).__name__}: {exc}")
        return False
    print(f"  ✓ PASS  {label}")
    return True


def verify_files(title: str, paths: list[str]) -> bool:
    """Check that each path exists under agentic-ai/."""
    print(f"\nVerifying {title}...")
    all_exist = True
    for rel in paths:
        if (ROOT / rel).exists():
            print(f"  ✓ PASS  {rel}")
        else:
            print(f"  ✗ FAIL  {rel} missing")
            all_exist = False
    return all_exist


def main() -> int:
    """Run all verification checks for one family."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--family", choices=sorted(FAMILIES), default="core",
        help="install family to verify (default: core)",
    )
    family = parser.parse_args().family

    print("=" * 60)
    print("AI Agent Learning Series - Installation Verification")
    print(f"Family: {family}  (Python {sys.version.split()[0]})")
    print("=" * 60)

    results: dict[str, bool] = {}

    print("\nVerifying module imports...")
    for name, modules in FAMILIES.items():
        for label, checks in modules:
            if name == family:
                results[label] = verify_imports(label, checks)
            else:
                target = "" if name == "core" else f" {name}"
                print(f"  - SKIP  {label}  (./INSTALL.sh{target})")

    results["Demo scripts"] = verify_files("demo scripts", DEMOS)
    results["Documentation"] = verify_files("documentation", DOCS)

    print("\n" + "=" * 60)
    print("Verification Summary")
    print("=" * 60)
    for component, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{component:45s} {status}")

    install = "./INSTALL.sh" if family == "core" else f"./INSTALL.sh {family}"
    if all(results.values()):
        print(f"\n✓ All {family} components verified successfully!")
        print("\nNext steps:")
        print(f"  1. Activate the environment created by {install}")
        print("  2. Run a demo in mock mode, as listed in README.md (Quick Start)")
        print("  3. See GETTING_STARTED.md for detailed instructions")
        return 0

    print(f"\n✗ Some {family} components failed verification")
    print(f"Re-run {install} in this environment, then see README.md")
    return 1


if __name__ == "__main__":
    sys.exit(main())
