# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/detect.py (5)."""
from pathlib import Path

import pytest

from module11.release import artifacts as A
from module11.release import detect as D
from module11.release import stage as cfg

ROOT = Path(__file__).resolve().parents[1]
RULES, UNKNOWN, _ = cfg.change_types(ROOT)
BASE = {aid: "sha256:" + "0" * 64 for aid in A.compute(ROOT / "tests/fixtures/module11/harness_min").ids()}
TOOLS = {t: {"timeoutSeconds": 10, "memoryMb": 256} for t in
         ["check_rollback_readiness", "get_blast_radius", "get_deployment_history", "get_service_logs",
          "query_metrics", "record_incident_note", "search_runbooks"]}


def change(*ids, base=BASE):
    new = dict(base)
    for aid in ids:
        new[aid] = "sha256:" + "1" * 64
    return new


def plan(*ids, base=BASE, tools=None, mv="2026-10-19T10:00:00Z/1111111", **kw):
    return D.detect(change(*ids, base=base), base, RULES, UNKNOWN, base_manifest_version=mv,
                    new_tools=tools or TOOLS, base_tools=TOOLS, **kw)


ROWS = [
    # ids, kinds, image, handlers, bundle, runtimeVersion, scope, approval, rollout, releaseKind
    (["instructions/companion"], ["instructions"], False, [], True, "config", "full", "default", "canary", "deploy"),
    (["tool/search_runbooks/description"], ["tool_description"], False, [], True, "config", "full", "default", "canary", "deploy"),
    (["tool/search_runbooks/meta"], ["tool_meta"], False, [], True, "config", "full", "default", "canary", "deploy"),
    (["tool/search_runbooks/schema"], ["tool_schema"], False, ["search_runbooks"], True, "config", "full", "default", "canary", "deploy"),
    (["tool/query_metrics/handler"], ["tool_handler"], False, ["query_metrics"], False, "config", "tag:query_metrics", "default", "canary", "deploy"),
    (["tool/query_metrics/permissions"], ["tool_permissions"], False, [], False, "none", "tag:query_metrics", "default", "full", "deploy"),
    (["orchestration"], ["orchestration"], False, [], True, "config", "full", "default", "canary", "deploy"),
    (["model"], ["model"], False, [], True, "config", "full", "required", "shadow-then-canary", "deploy"),
    (["guardrail"], ["guardrail"], False, [], False, "config", "full", "default", "canary", "deploy"),
    (["retrieval"], ["retrieval_corpus"], False, [], False, "config", "tag:retrieval", "default", "canary", "deploy"),
    (["evidence"], ["evidence"], False, [], False, "config", "full", "default", "canary", "deploy"),
    (["external_surfaces"], ["external_surfaces"], False, [], False, "none", "full", "default", "none", "deploy"),
    (["runtime/code"], ["runtime_code"], True, [], False, "image", "full", "default", "canary", "deploy"),
    (["edge/code"], ["edge_code"], False, [], False, "none", "smoke", "default", "full", "deploy"),
    (["flags"], ["flags"], False, [], False, "none", "full", "default", "full", "deploy"),
    (["evaluation/suite"], ["evaluation_suite"], False, [], False, "none", "rebaseline", "default", "none", "rebaseline"),
    (["evaluation/thresholds"], ["evaluation_suite"], False, [], False, "none", "rebaseline", "default", "none", "rebaseline"),
    (["evaluation/evaluators"], ["evaluator_set"], False, [], False, "none", "evalset", "required", "none", "evalset"),
    (["pipeline"], ["pipeline"], False, [], False, "none", "smoke", "default", "none", "deploy"),
    (["infrastructure"], ["infrastructure"], False, [], False, "maybe", "smoke", "default", "canary-if-runtime-changed", "deploy"),
]


@pytest.mark.parametrize("ids,kinds,image,handlers,bundle,runtime,scope,approval,rollout,kind", ROWS,
                         ids=[r[1][0] + ":" + r[0][0] for r in ROWS])
def test_change_type_rows(ids, kinds, image, handlers, bundle, runtime, scope, approval, rollout, kind):
    p = plan(*ids)
    assert (p.kinds, p.imageRebuild, p.handlerBuild, p.bundleVersion, p.runtimeVersion, p.evalScope, p.approval,
            p.rollout, p.releaseKind) == (kinds, image, handlers, bundle, runtime, scope, approval, rollout, kind)


def test_tool_meta_rebuilds_handler_only_on_limits_change():
    tools = {**TOOLS, "search_runbooks": {"timeoutSeconds": 20, "memoryMb": 256}}
    assert plan("tool/search_runbooks/meta", tools=tools).handlerBuild == ["search_runbooks"]
    assert plan("tool/search_runbooks/meta").handlerBuild == []


def test_model_requires_approval_five_repeats_and_model_profile():
    p = plan("model")
    assert p.repeatsOverride == {"default": 5, "adversarial": 5}
    assert D.profile_for(p) == "model"
    assert D.profile_for(plan("instructions/companion"), "demo") == "demo"
    assert D.profile_for(plan("flags")) is None


def test_guardrail_runs_adversarial_at_five_repeats():
    assert plan("guardrail").repeatsOverride == {"adversarial": 5}


def test_mixed_changes_merge_to_the_maximum():
    p = plan("tool/query_metrics/handler", "model", "runtime/code")
    assert p.imageRebuild and p.handlerBuild == ["query_metrics"] and p.bundleVersion
    assert (p.runtimeVersion, p.evalScope, p.approval, p.rollout) == ("image", "full", "required", "shadow-then-canary")
    assert plan("tool/query_metrics/handler", "retrieval").evalScope == "full"  # two subsets widen to full


def test_unknown_artifact_is_conservative():
    p = plan("brand-new/thing")
    assert p.kinds == ["unknown"] and p.imageRebuild and p.bundleVersion
    assert (p.runtimeVersion, p.evalScope, p.approval) == ("image", "full", "required")
    assert set(p.handlerBuild) == set(TOOLS)


def test_revert_gives_noop():
    p = D.detect(dict(BASE), BASE, RULES, UNKNOWN)
    assert p.releaseKind == "noop" and p.kinds == [] and p.rollout == "none"


def test_bootstrap_has_no_base_and_is_c16_exempt():
    p = D.detect(change("flags"), None, RULES, UNKNOWN)
    assert "bootstrap: no base manifest" in p.notes
    D.enforce_c16(p, RULES, UNKNOWN, has_base=False)


def test_pipeline_variables():
    assert plan("model").variables() == {"IMAGE_REBUILD": "false", "RELEASE_KIND": "deploy", "EVAL_SCOPE": "full",
                                         "APPROVAL": "required", "ROLLOUT": "shadow-then-canary"}


# ---------------------------------------------------------------- C16
@pytest.mark.parametrize("canary_id", ["instructions/companion", "infrastructure", "model"])
@pytest.mark.parametrize("stage_wide", ["flags", "edge/code"])
def test_c16_rejects_stage_wide_kinds_in_canary_plans(canary_id, stage_wide):
    p = plan(canary_id, stage_wide)
    assert p.rollout in D.CANARY_CLASS
    with pytest.raises(D.ChangePlanRejected, match="land them in their own MR"):
        D.enforce_c16(p, RULES, UNKNOWN)


def test_infrastructure_with_instructions_passes_with_both_arms_note():
    p = plan("infrastructure", "instructions/companion")
    D.enforce_c16(p, RULES, UNKNOWN)
    assert any("infrastructure: applies to both arms immediately" in n for n in p.notes)


# ---------------------------------------------------------------- stricter / StaleBase / BaseAdvanced
@pytest.mark.parametrize("new_ids,old_ids,expected", [
    (["model"], ["instructions/companion"], True),                       # approval
    (["tool/query_metrics/handler"], ["instructions/companion"], False),  # full covers tag:*
    (["instructions/companion"], ["tool/query_metrics/handler"], True),   # scope widened
    (["tool/get_blast_radius/handler"], ["tool/query_metrics/handler"], True),
    (["instructions/companion"], ["flags"], True),                        # rollout more cautious
    (["evaluation/suite"], ["instructions/companion"], True),             # release kind differs
    (["flags"], ["instructions/companion"], False),
])
def test_stricter(new_ids, old_ids, expected):
    assert D.stricter(plan(*new_ids), plan(*old_ids)) is expected


def test_unchanged_base_proceeds_whatever_the_stage_runs():
    p = plan("instructions/companion")
    decision = D.check_base(p, p.baseManifestVersion, lambda: pytest.fail("no re-plan"), "staging")
    assert decision.action == "proceed" and decision.plan is p


def test_base_advanced_not_stricter_proceeds_with_old_plan():
    old = plan("instructions/companion", "tool/query_metrics/handler")
    new = plan("tool/query_metrics/handler", mv="2026-10-20T09:00:00Z/2222222")
    decision = D.check_base(old, new.baseManifestVersion, lambda: new, "prod")
    assert decision.action == "proceed" and decision.plan is old
    assert decision.effective_kinds == ["tool_handler"] and decision.log.startswith("BaseAdvanced(")


def test_base_advanced_stricter_fails_stale_base():
    old = plan("instructions/companion")
    new = plan("instructions/companion", "model", mv="2026-10-20T09:00:00Z/2222222")  # hotfix made model differ
    with pytest.raises(D.StaleBase):
        D.check_base(old, new.baseManifestVersion, lambda: new, "prod")


def test_noop_replan_ends_rollout_stage_deploy_but_staging_deploys():
    old = plan("instructions/companion")
    noop = D.detect(dict(BASE), BASE, RULES, UNKNOWN, base_manifest_version="2026-10-20T09:00:00Z/2222222")
    prod = D.check_base(old, noop.baseManifestVersion, lambda: noop, "prod")
    assert prod.action == "noop" and prod.log == "BaseAdvanced(noop)"
    assert D.check_base(old, noop.baseManifestVersion, lambda: noop, "staging").action == "proceed"


def test_mr_stage_replans_once_then_fails():
    old = plan("instructions/companion")
    new = plan("instructions/companion", "model", mv="2026-10-20T09:00:00Z/2222222")
    decision = D.check_base(old, new.baseManifestVersion, lambda: new, "mr-12", moves=1)
    assert decision.plan is new
    with pytest.raises(D.StaleBase):
        D.check_base(old, new.baseManifestVersion, lambda: new, "mr-12", moves=2)


# ---------------------------------------------------------------- queued C16
PROD = {"manifestVersion": "2026-10-19T10:00:00Z/1111111", "git": {"commit": "1111111" + "0" * 33}, "artifacts": BASE}
INSTR = {"manifestVersion": "2026-10-20T09:00:00Z/2222222", "git": {"commit": "2222222" + "0" * 33},
         "artifacts": change("instructions/companion")}


def _executions(*rows):
    return [{"pipelineExecutionId": eid, "status": status, "startTime": start,
             "sourceRevisions": [{"revisionId": commit}]} for eid, status, start, commit in rows]


def test_c16_base_is_the_newest_earlier_in_flight_release():
    executions = _executions(("e1", "InProgress", "2026-10-20T09:01:00", INSTR["git"]["commit"]),
                             ("e2", "InProgress", "2026-10-20T09:30:00", "3333333" + "0" * 33),
                             ("e0", "Succeeded", "2026-10-19T10:01:00", PROD["git"]["commit"]))
    base, source = D.c16_base(executions, {INSTR["git"]["commit"]: INSTR}.__getitem__, PROD, own_execution_id="e2")
    assert base is INSTR and source == "in-flight"


def test_queued_flags_commit_passes_c16_against_c16_base_while_plan_uses_prod():
    flags_commit = change("instructions/companion", "flags")  # main: instructions (queued ahead) + flags
    c16_plan = D.detect(flags_commit, INSTR["artifacts"], RULES, UNKNOWN)
    D.enforce_c16(c16_plan, RULES, UNKNOWN)  # flags only against the release ahead -> full
    prod_plan = D.detect(flags_commit, BASE, RULES, UNKNOWN, base_manifest_version=PROD["manifestVersion"],
                         c16_base=INSTR["manifestVersion"])
    assert prod_plan.rollout == "canary"  # approval, scope and rollout still against prod /current
    # At prod deploy, the release ahead promoted: the base moved, re-plan is flags-only.
    replan = D.detect(flags_commit, INSTR["artifacts"], RULES, UNKNOWN, base_manifest_version=INSTR["manifestVersion"])
    decision = D.check_base(prod_plan, INSTR["manifestVersion"], lambda: replan, "prod")
    D.deploy_c16(decision, RULES, UNKNOWN, "prod", prod_plan.c16Base, INSTR["manifestVersion"])
    stable = {"runtimeVersion": "13", "runtimeIdentity": "i", "runtimeDigest": "r", "imageDigest": "d", "infrastructure": "x"}
    assert D.resolve_path(decision.plan, "prod", dict(stable), stable, decision.effective_kinds).path == "unchanged"
    # If it rolled back instead, prod /current never moved: step 1 rejects with the queued message.
    stayed = D.check_base(prod_plan, PROD["manifestVersion"], lambda: pytest.fail("no re-plan"), "prod")
    with pytest.raises(D.ChangePlanRejected, match="queued ahead of this one did not promote"):
        D.deploy_c16(stayed, RULES, UNKNOWN, "prod", prod_plan.c16Base, PROD["manifestVersion"])


def test_reverse_order_instructions_behind_flags():
    flags_rel = {"manifestVersion": "2026-10-20T09:00:00Z/4444444", "artifacts": change("flags")}
    commit = change("flags", "instructions/companion")
    D.enforce_c16(D.detect(commit, flags_rel["artifacts"], RULES, UNKNOWN), RULES, UNKNOWN)


def test_c16_base_falls_back_to_prod_current():
    assert D.c16_base([], lambda c: None, PROD) == (PROD, "prod-current")

    def boom(_):
        raise RuntimeError("ListPipelineExecutions failed")

    executions = _executions(("e1", "InProgress", "2026-10-20T09:01:00", INSTR["git"]["commit"]))
    assert D.c16_base(executions, boom, PROD) == (PROD, "fallback")


# ---------------------------------------------------------------- resolve_path
STABLE = {"runtimeVersion": "13", "runtimeIdentity": "sha256:id", "runtimeDigest": "sha256:rt",
          "imageDigest": "sha256:img", "infrastructure": "sha256:inf"}


def test_resolve_path_single_bootstrap_unchanged():
    p = plan("instructions/companion")
    assert D.resolve_path(p, "staging", {**STABLE, "runtimeVersion": "14"}, STABLE).path == "single"
    assert D.resolve_path(p, "mr-7", {**STABLE, "runtimeVersion": "14"}, STABLE).path == "single"
    assert D.resolve_path(p, "prod", {**STABLE, "runtimeVersion": "14"}, None).path == "bootstrap"
    assert D.resolve_path(plan("flags"), "prod", dict(STABLE), STABLE).path == "unchanged"


def test_equivalent_after_rollback_and_rollout_otherwise():
    p = plan("flags")
    res = D.resolve_path(p, "prod", {**STABLE, "runtimeVersion": "15"}, STABLE)
    assert res.path == "equivalent" and res.logs == ["EquivalentRuntimeRetarget(13, 15)"]
    infra = plan("infrastructure")
    res = D.resolve_path(infra, "prod", {**STABLE, "runtimeVersion": "15", "infrastructure": "sha256:other"}, STABLE)
    assert res.path == "rollout"
    res = D.resolve_path(plan("instructions/companion"), "prod",
                         {**STABLE, "runtimeVersion": "15", "runtimeIdentity": "sha256:new"}, STABLE)
    assert res.path == "rollout"


def test_rollout_with_stage_wide_kinds_is_rejected():
    with pytest.raises(D.ChangePlanRejected, match="the deploy produced a runtime change: runtimeIdentity"):
        D.resolve_path(plan("flags"), "prod", {**STABLE, "runtimeVersion": "15", "runtimeIdentity": "sha256:x"}, STABLE)


def test_full_plan_with_new_version_is_upgraded_to_rollout():
    res = D.resolve_path(plan("tool/query_metrics/permissions"), "prod",
                         {**STABLE, "runtimeVersion": "15", "runtimeIdentity": "sha256:x"}, STABLE)
    assert res.path == "rollout" and res.logs == ["RolloutUpgraded(full -> canary)"]


# ---------------------------------------------------------------- main ahead of prod
def test_main_ahead_of_prod_after_rollback():
    main_declared = change("instructions/companion")  # rolled back on prod, still on main
    mr = change("instructions/companion", "flags")
    merged = D.merged_hashes(main_declared, mr, ["flags"])
    base, source = D.c16_base(_executions(("e9", "Succeeded", "x", "y")), lambda c: None, PROD)
    assert source == "prod-current"
    p = D.detect(merged, base["artifacts"], RULES, UNKNOWN)
    assert p.kinds == ["flags", "instructions"]
    rng = D.commit_range(PROD["git"]["commit"], "5555555" + "0" * 33)
    with pytest.raises(D.ChangePlanRejected) as exc:
        D.enforce_c16(p, RULES, UNKNOWN, own_kinds=["flags"], commit_range=rng)
    assert str(exc.value).startswith("main is ahead of prod by instructions from 1111111..5555555 (not promoted)")
    # The same flags commit at Analyze: instructions is absent from its own diff vs its parent.
    own = D.detect(mr, main_declared, RULES, UNKNOWN).kinds
    with pytest.raises(D.ChangePlanRejected, match="main is ahead of prod"):
        D.enforce_c16(p, RULES, UNKNOWN, own_kinds=own, commit_range=rng)
    # An MR that itself changes instructions and flags gets the own-MR message.
    with pytest.raises(D.ChangePlanRejected, match="land them in their own MR"):
        D.enforce_c16(p, RULES, UNKNOWN, own_kinds=["instructions", "flags"], commit_range=rng)


def test_after_revert_the_flags_mr_passes_and_deploys_full():
    main_declared = dict(BASE)  # revert merged: main == prod /current
    merged = D.merged_hashes(main_declared, change("flags"), ["flags"])
    p = D.detect(merged, BASE, RULES, UNKNOWN)
    D.enforce_c16(p, RULES, UNKNOWN, own_kinds=["flags"])
    assert p.rollout == "full"
    assert D.resolve_path(p, "prod", dict(STABLE), STABLE).path == "unchanged"


def test_fix_forward_in_flight_is_the_c16_base():
    fix = {"manifestVersion": "2026-10-20T11:00:00Z/6666666", "artifacts": change("instructions/companion")}
    executions = _executions(("e5", "InProgress", "2026-10-20T11:01:00", "6666666" + "0" * 33))
    base, source = D.c16_base(executions, lambda c: fix, PROD)
    merged = D.merged_hashes(change("instructions/companion"), change("instructions/companion", "flags"), ["flags"])
    D.enforce_c16(D.detect(merged, base["artifacts"], RULES, UNKNOWN), RULES, UNKNOWN, own_kinds=["flags"])
    assert source == "in-flight"


def test_merged_hashes_takes_only_mr_changed_artifacts():
    main = {"a": "1", "b": "1"}
    assert D.merged_hashes(main, {"a": "2", "b": "9"}, ["a"]) == {"a": "2", "b": "1"}
    assert D.merged_hashes(main, {"b": "1"}, ["a"]) == {"b": "1"}
