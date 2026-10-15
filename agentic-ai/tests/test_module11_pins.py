# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for module11/release/pins.py."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from module11.release import detect as D
from module11.release import pins as P
from module11.release import stage as cfg

ROOT = Path(__file__).resolve().parents[1]
REGION, ACCOUNT = "us-east-1", "111122223333"
BUCKET = "m11-release-111122223333-us-east-1"
REFS = {"evidence": f"arn:aws:s3:::{BUCKET}/evidence/*", "snapshot": f"arn:aws:s3:::{BUCKET}/snapshots/policy/*",
        "side_effects": "arn:aws:s3:::m11-prod-artifacts/side-effects/prod/*",
        "memory": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/m11_prod-abc"}
TOOLS = ["check_rollback_readiness", "get_blast_radius", "get_deployment_history", "get_service_logs",
         "query_metrics", "record_incident_note", "search_runbooks"]


def permissions():
    return {t: json.loads((ROOT / f"module11/harness/agent/tools/{t}/permissions.json").read_text(encoding="utf-8"))
            for t in TOOLS}


def arns(version):
    return {t: f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:m11-prod-{t}:{version}" for t in TOOLS}


def declared(deadline=150):
    return {"orchestration": {"deadlineSeconds": deadline}}


def own(version, deadline=150, model="us.openai.gpt-5.6-luna"):
    models = {"companion": {"modelId": model}, "planner": {"modelId": model}}
    static = P.static_pins(declared(deadline), models, permissions(), REFS, REGION, ACCOUNT)
    return P.Pins(toolArns=arns(version), modelResources=static.modelResources, guardrailVersion=str(version),
                  deadlineSeconds=deadline, toolStatements=static.toolStatements)


def store(stage="prod"):
    objects = {}

    def deployed(mv, own_pins, granted_pins):
        uri = f"s3://{BUCKET}/release-inputs/{stage}/{mv}.pins.json"
        objects[uri] = P.pins_object(stage, mv, own_pins, granted_pins)
        return {"manifestVersion": mv, "deployment": {stage: {"pinsUri": uri}}}

    return objects, deployed


def test_least_privilege_statements_and_refusals():
    statements = P.least_privilege(permissions()["record_incident_note"], REFS)
    assert statements == [
        {"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": [REFS["side_effects"]]},
        {"Effect": "Allow", "Action": ["bedrock-agentcore:BatchCreateMemoryRecords"], "Resource": [REFS["memory"]]}]
    with pytest.raises(P.PinsViolation):
        P.least_privilege([{"actions": ["s3:*"], "resource": "evidence"}], REFS)
    with pytest.raises(P.PinsViolation):
        P.least_privilege([{"actions": ["s3:PutObject"], "resource": "evidence"}], REFS)


def test_static_pins_read_models_and_statements_and_invent_no_arns():
    models = cfg.models(ROOT)
    static = P.static_pins(declared(), models, permissions(), REFS, REGION, ACCOUNT)
    assert static.toolArns == {} and static.guardrailVersion is None and static.deadlineSeconds == 150
    profile = models["companion"]["modelId"]
    assert f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{profile}" in static.modelResources
    assert f"arn:aws:bedrock:*::foundation-model/{models['companion']['foundationModelId']}" in static.modelResources
    assert set(static.toolStatements) == set(TOOLS)


def test_pins_of_returns_own_only():
    objects, deployed = store()
    o = own(9)
    g = P.granted(P.union(own(8), o), arns(9))
    current = deployed("v9", o, g)
    got = P.pins_of(current, "prod", objects.__getitem__)
    assert got.toolArns == arns(9) and got.tool_arn_set() != g.tool_arn_set()
    with pytest.raises(P.PinsViolation):
        P.pins_of({"deployment": {}}, "prod", objects.__getitem__)


def test_post_up_violation_rules():
    retained = P.union(own(9), P.static_pins(declared(), {"c": {"modelId": "m"}}, {}, REFS, REGION, ACCOUNT))
    g = P.granted(retained, arns(10))
    P.check_after_up(g, retained, arns(10), P.Pins(toolArns=arns(10)), arns(10))
    with pytest.raises(P.PinsViolation, match="outside retained"):
        P.check_after_up(P.union(g, P.Pins(toolArns={"x": "arn:other:1"})), retained, arns(10),
                         P.Pins(toolArns=arns(10)), arns(10))
    with pytest.raises(P.PinsViolation, match="own != deployed"):
        P.check_after_up(g, retained, arns(10), P.Pins(toolArns=arns(10)), arns(11))


def test_union_keeps_both_releases_and_prune_resets_to_current():
    retained = P.union(own(9, model="m-old"), own(10, model="m-new"))
    assert retained.tool_arn_set() == set(arns(9).values()) | set(arns(10).values())
    assert any("m-old" in r for r in retained.modelResources) and any("m-new" in r for r in retained.modelResources)
    pruned = P.prune(own(10, model="m-new"), arns(10))
    assert pruned.tool_arn_set() == set(arns(10).values())
    assert not any("m-old" in r for r in pruned.modelResources)


def test_prune_unsafe_and_rollout_in_progress():
    P.check_prune_safe("14", {"stable": "14", "candidate": "14"}, {"stable": {"version": "14"}})
    with pytest.raises(P.PruneUnsafe):
        P.check_prune_safe("14", {"stable": "14", "candidate": "15"}, {})
    with pytest.raises(P.PruneUnsafe):
        P.check_prune_safe("14", {"stable": "14"}, {"candidate": {"version": "15"}})
    for state in ("IDLE", "PROMOTED", "ROLLED_BACK", None):
        P.check_rollout_idle(state)
    with pytest.raises(P.RolloutInProgress):
        P.check_rollout_idle("OBSERVING")


def test_case1_promoted_then_prune_grants_exactly_current_own():
    pruned = P.prune(own(10), arns(10))
    assert pruned.tool_arn_set() == own(10).tool_arn_set()


@pytest.mark.parametrize("with_prunes", [True, False])
def test_case2_and_case3_no_accumulation_and_size(with_prunes):
    objects, deployed = store()
    current = deployed("v7", own(7), P.granted(own(7), arns(7)))
    for version in (8, 9, 10):
        retained = P.union(P.pins_of(current, "prod", objects.__getitem__),
                           P.static_pins(declared(), cfg.models(ROOT), permissions(), REFS, REGION, ACCOUNT))
        g = P.granted(retained, arns(version), str(version))
        older = {a for v in range(7, version - 1) for a in arns(v).values()}
        assert not (g.tool_arn_set() & older), f"v{version} still grants a pin older than its predecessor"
        policy = P.runtime_role_policy(g, arns(version), "arn:aws:logs:us-east-1:1:log-group:/aws/x")
        assert P.policy_chars(policy) <= P.MAX_RUNTIME_POLICY_CHARS
        current = deployed(f"v{version}", own(version), P.prune(own(version), arns(version)) if with_prunes else g)


def test_case4_adapter_timeout_follows_the_maximum_deadline():
    canary = P.union(own(9, deadline=150), P.Pins(deadlineSeconds=60))
    assert P.adapter_timeout(canary) == 180
    assert P.adapter_timeout(P.prune(own(9, deadline=150), arns(9))) == 180  # after ROLLED_BACK and prune
    assert P.adapter_timeout(P.prune(own(10, deadline=60), arns(10))) == 90  # after PROMOTED and prune
    assert P.adapter_timeout(P.union(own(9, deadline=150), P.Pins(deadlineSeconds=300))) == 330


def test_after_rollback_staging_retains_its_own_current():
    objects, deployed = store("staging")
    staging_current = deployed("v8", own(8), P.granted(own(8), arns(8)))  # staging left on rolled-back v8
    plan = D.ChangePlan("prod-v7", None, ["instructions"], False, [], True, "config", "full", {}, "default",
                        "canary", "deploy")
    decision = D.check_base(plan, "prod-v7", lambda: pytest.fail("no re-plan"), "staging")
    assert decision.action == "proceed"  # StaleBase passes: prod /current unchanged
    retained = P.union(P.pins_of(staging_current, "staging", objects.__getitem__),
                       P.static_pins(declared(), cfg.models(ROOT), permissions(), REFS, REGION, ACCOUNT))
    assert set(arns(8).values()) <= retained.tool_arn_set()


ROLLED_BACK = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
EPOCH = 1792508400.0                                   # 2026-10-20T15:00:00Z, the controller's format
RID = "r-0123456789abcdef"
PINS150 = {"granted": {"deadlineSeconds": 150}}


def _driver(**fields):
    return {"rolloutId": RID, "pid": 4242, "startedAt": "2026-10-20T14:56:00+00:00", **fields}


def test_purge_ready_at_reads_the_candidates_own_pins():
    stable, candidate = own(9, deadline=150), own(10, deadline=300)
    candidate_pins = P.pins_object("prod", "v10", candidate, P.union(stable, candidate))
    applied_after_prune = P.pins_object("prod", "v9", stable, P.prune(stable, arns(9)))
    rollout = {"rolloutId": RID, "state": "ROLLED_BACK", "rolledBackAt": EPOCH}
    assert EPOCH == ROLLED_BACK.timestamp()
    assert P.purge_ready_at(rollout, candidate_pins, None) == ROLLED_BACK + timedelta(seconds=330)
    assert P.purge_ready_at(rollout, applied_after_prune) == ROLLED_BACK + timedelta(seconds=180)
    # The aware-datetime case.
    assert P.purge_ready_at({**rollout, "rolledBackAt": ROLLED_BACK}, candidate_pins) == \
        ROLLED_BACK + timedelta(seconds=330)
    driver = _driver(finishedAt="2026-10-20T15:02:00Z")
    assert P.purge_ready_at(rollout, candidate_pins, driver) == ROLLED_BACK + timedelta(minutes=2)


def test_purge_ready_at_contract_cases():
    rollout = {"rolloutId": RID, "state": "ROLLED_BACK", "rolledBackAt": EPOCH}
    # 1. finishedAt of this rollout's driver.
    assert P.purge_ready_at(rollout, PINS150, _driver(finishedAt="2026-10-20T15:03:00+00:00")) == \
        ROLLED_BACK + timedelta(minutes=3)
    # 2. killedAt + deadline + 30.
    assert P.purge_ready_at(rollout, PINS150, _driver(killedAt="2026-10-20T15:01:00+00:00")) == \
        ROLLED_BACK + timedelta(minutes=1, seconds=180)
    # 3. this rollout's driver is still running.
    assert P.purge_ready_at(rollout, PINS150, _driver()) is None
    # 4. another rollout's driver state, or none: the terminal time + deadline + 30.
    other = {**_driver(finishedAt="2026-10-20T15:03:00+00:00"), "rolloutId": "r-ffffffffffffffff"}
    assert P.purge_ready_at(rollout, PINS150, other) == ROLLED_BACK + timedelta(seconds=180)
    assert P.purge_ready_at(rollout, PINS150, None) == ROLLED_BACK + timedelta(seconds=180)
    assert P.purge_ready_at(rollout, PINS150, {}) == ROLLED_BACK + timedelta(seconds=180)
    # PROMOTED: promotedAt (a float, as controller.decide writes it).
    promoted = {"rolloutId": RID, "state": "PROMOTED", "promotedAt": EPOCH + 60.5}
    assert P.purge_ready_at(promoted, PINS150) == ROLLED_BACK + timedelta(seconds=240.5)
    # Decimal, as DynamoDB returns numbers.
    assert P.purge_ready_at({**rollout, "rolledBackAt": Decimal("1792508400")}, PINS150) == \
        ROLLED_BACK + timedelta(seconds=180)


@pytest.mark.parametrize("bad", [True, None, "not a time", float("nan"), "2026-10-20T15:00:00",
                                 datetime(2026, 10, 20, 15, 0), {"at": 1}, Decimal("NaN")])
def test_purge_ready_at_bad_terminal_timestamps_raise_pins_violation(bad):
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at({"rolloutId": RID, "rolledBackAt": bad}, PINS150)


def test_ts_formats():
    assert P.ts(EPOCH) == ROLLED_BACK and P.ts(Decimal("1792508400")) == ROLLED_BACK and P.ts(1792508400) == ROLLED_BACK
    assert P.ts("2026-10-20T15:00:00Z") == ROLLED_BACK
    assert P.ts("2026-10-20T11:00:00-04:00") == ROLLED_BACK
    assert P.ts(ROLLED_BACK) is ROLLED_BACK
    for bad in (True, None, "not a time", float("nan"), "2026-10-20T15:00:00", datetime(2026, 10, 20), [1]):
        with pytest.raises(P.PinsViolation):
            P.ts(bad)


@pytest.mark.parametrize("value,ok", [(150, True), (Decimal("150"), True), (150.5, False), (True, False), (0, False),
                                      (-5, False), (None, False), (Decimal("150.5"), False), (Decimal("Infinity"), False),
                                      ("150", False), (86_400, True), (86_401, False), (10**12, False)])
def test_granted_deadline_seconds(value, ok):
    pins = {"granted": {"deadlineSeconds": value}} if value is not None else {"granted": {}}
    if ok:
        assert P.granted_deadline_seconds(pins) == int(value)
    else:
        with pytest.raises(P.PinsViolation):
            P.granted_deadline_seconds(pins)


def test_overflow_and_non_mapping_granted_are_pins_violations():
    rollout = {"rolloutId": RID, "rolledBackAt": EPOCH}
    # A huge deadline, a terminal time near datetime.max, and a non-mapping granted.
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, {"granted": {"deadlineSeconds": 10**12}})
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at({"rolloutId": RID, "rolledBackAt": 253402300799}, PINS150)
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, {"granted": "x"})
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, {})                    # missing deadline, in every case
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, {}, _driver(finishedAt="2026-10-20T15:03:00+00:00"))


def test_driver_timestamps_are_checked_against_started_at():
    rollout = {"rolloutId": RID, "rolledBackAt": EPOCH}
    # (3): finishedAt/killedAt earlier than startedAt, or no startedAt, is PinsViolation.
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, PINS150, _driver(finishedAt="2026-10-20T14:50:00+00:00"))
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, PINS150, _driver(killedAt="2026-10-20T14:50:00+00:00"))
    no_start = {"rolloutId": RID, "finishedAt": "2026-10-20T15:03:00+00:00"}
    with pytest.raises(P.PinsViolation):
        P.purge_ready_at(rollout, PINS150, no_start)
    with pytest.raises(P.PinsViolation):              # a naive driver timestamp
        P.purge_ready_at(rollout, PINS150, _driver(finishedAt="2026-10-20T15:03:00"))
