# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Names used by the evaluation runner and the code-based evaluator's stack resources.

Kept apart from `naming.py` on purpose: `naming` is in the rollout simulator's import closure, so an edit there
would make `controller_sim_report.json` stale (test_controller_sim_report_current) without changing the controller.
"""
from __future__ import annotations

from . import naming

EVALUATOR_SHORT = "eval-no-credential-leakage"
EVALUATOR_LOGICAL = "custom.no_credential_leakage"


def evaluator_function(stage: str) -> str:
    """The code-based evaluator's Lambda (its role and log group carry the same name)."""
    return naming.function(stage, EVALUATOR_SHORT)


def evaluator_name(stage: str) -> str:
    """AgentcoreEvaluator name: `^[a-zA-Z][a-zA-Z0-9_]{0,47}$`, so underscores only."""
    return f"m11_{naming.stagekey(stage)}_eval_no_credential_leakage"


def runtime_log_group(runtime_id: str, endpoint: str) -> str:
    """`/aws/bedrock-agentcore/runtimes/<runtimeId>-<endpoint>`."""
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-{endpoint}"


def eval_endpoint(stage: str) -> str:
    """The endpoint an evaluation measures: prod and dev serve `stable`, staging `staging`, mr-* `preview`."""
    return naming.endpoints_for(stage)[0]


def session_id_for_eval(run_id: str, scenario: str, repeat: int, suffix: str) -> str:
    """`m11-eval-<runId>-<scenario>-<r>-<uuid8>`; AgentCore accepts 33-256 characters."""
    value = f"m11-eval-{run_id}-{scenario}-{int(repeat)}-{suffix}"
    if not 33 <= len(value) <= 256:
        raise naming.NameError_(f"runtime session ID of {len(value)} characters is outside 33..256")
    return value
