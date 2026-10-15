"""
module6/agent.py
================
Thin wrapper over the DevOps Companion Temporal workflow.

Unlike Modules 2-4 (which use LangGraph agents), Module 6 drives
workflows directly via the Temporal SDK. The "agent" here is the
workflow orchestrator — it starts workflows, sends signals, and
queries state.

FRAMEWORK: Temporal Python SDK (not LangGraph)
MODEL: Amazon Bedrock (Claude) — used inside activities, not at the
       orchestration level.
"""

from __future__ import annotations

from typing import Any

from module6.workflows.devops_companion import DevOpsCompanionWorkflow
from module6.mock.temporal_mocks import get_mock_temporal_client


def create_workflow_orchestrator(verbose: bool = True) -> DevOpsCompanionWorkflow:
    """Create a DevOps Companion workflow orchestrator.

    Parameters
    ----------
    verbose : bool
        Print workflow steps during execution.

    Returns
    -------
    DevOpsCompanionWorkflow
        Configured workflow orchestrator. It is always backed by the
        in-process MockTemporalClient (the Module 6 teaching layer),
        whatever TEMPORAL_API_KEY is set to. Real Temporal workflows and
        workers live in the top-level orchestration/ package
        (orchestration.client and orchestration.worker).
    """
    client = get_mock_temporal_client()

    if verbose:
        print("  [Module 6] DevOps Companion Workflow Orchestrator")
        print("  [Framework] Temporal Python SDK")
        print(f"  [Task Queue] {client.get_worker_info()['task_queue']}")
        print(f"  [Workers] {len(client.get_worker_info()['workers'])} ECS Fargate tasks")
        print(f"  [Mode] {'MOCK' if client.get_worker_info()['mock_mode'] else 'LIVE'}")
        print()

    return DevOpsCompanionWorkflow()


def run_full_pipeline(
    repo_path: str = "/app/nodejs-api",
    region: str = "us-east-1",
    auto_approve: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """Run the full DevOps Companion pipeline end-to-end.

    Parameters
    ----------
    repo_path : str
        Repository path to analyze.
    region : str
        AWS region.
    auto_approve : bool
        If True, automatically approve at the human gate.
    verbose : bool
        Print progress.

    Returns
    -------
    dict
        Full pipeline results including all activity outputs.
    """
    wf = create_workflow_orchestrator(verbose=verbose)
    results: dict[str, Any] = {"stages": {}}

    # Start workflow
    start = wf.start(repo_path, region)
    results["workflow_id"] = start["workflow_id"]
    if verbose:
        print(f"  Started workflow: {start['workflow_id']}")

    # Analysis
    analysis_result = wf.run_analysis(repo_path, region)
    results["stages"]["analysis"] = analysis_result
    if verbose:
        print("  ✓ analyze_repository (checkpoint 1)")
        print("  ✓ map_dependencies (checkpoint 2)")

    # CDK Generation
    cdk_result = wf.run_generation(analysis_result["dependencies"])
    results["stages"]["generation"] = cdk_result
    if verbose:
        print("  ✓ generate_cdk (checkpoint 3)")

    # Parallel verification (static security scan + compliance check)
    verification = wf.run_parallel_verification(cdk_result)
    results["stages"]["verification"] = verification
    if verbose:
        print("  ✓ static_security_scan (checkpoint 4a)")
        print("  ✓ compliance_check (checkpoint 4b)")

    # Human approval gate
    pause_result = wf.pause_for_approval()
    results["stages"]["approval_gate"] = pause_result
    if verbose:
        print(f"  ⏸ Waiting for approval signal (workflow: {pause_result['workflow_id']})")

    if auto_approve:
        approval = wf.submit_approval(True, "auto-approver", "Auto-approved for demo")
        results["stages"]["approval"] = approval
        if verbose:
            print("  ✓ Approval signal received — resuming workflow")
    else:
        results["paused"] = True
        results["signal_target"] = pause_result["workflow_id"]
        return results

    # Deployment
    deploy_result = wf.run_deployment()
    results["stages"]["deployment"] = deploy_result
    if verbose:
        print("  ✓ deploy_to_ecs (checkpoint 5)")

    # Smoke tests
    test_result = wf.run_smoke_tests(deploy_result)
    results["stages"]["smoke_tests"] = test_result
    if verbose:
        print("  ✓ smoke_tests (checkpoint 6)")

    # Observability
    obs_result = wf.run_observability_setup(deploy_result)
    results["stages"]["observability"] = obs_result
    if verbose:
        print("  ✓ setup_observability (checkpoint 7)")

    # Final status
    results["status"] = wf.get_status()
    results["paused"] = False
    if verbose:
        print(f"\n  Pipeline complete. Status: {results['status']['status']}")

    return results
