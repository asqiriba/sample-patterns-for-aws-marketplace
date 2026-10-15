# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rollback controller: the pure state machine, the orchestration against fake
ports, the pooled rate rule, the seeded simulator and the committed simulation report.

M11_SLOW_TESTS=1 runs the prod and model grid (test_null_effect_false_rollback,
test_prod_profile_power). Both flags are read at import, before the isolate_module11 fixture
clears M11_* variables for the test body.
"""
import copy
import json
import os
import re
from pathlib import Path

import pytest

from module11.release import controller as ctl
from module11.release import rollout_sim as sim
from module11.release import signals as sig
from module11.release import stats
from module11.release.router_spec import CANDIDATE_WARMUP_SECONDS, PROFILES, WATCH_TOOLS

ROOT = Path(__file__).resolve().parents[1]
SLOW = os.environ.get("M11_SLOW_TESTS") == "1"
SIM_RUNS = os.environ.get("M11_SIM_RUNS")
RID = "r-0123456789abcdef"
DIGEST = "sha256:" + "a" * 64
STABLE_ENTRY = {"version": 7, "manifestVersion": "2026-10-12T09:14:55Z/3c8e1b0", "behaviorDigest": "sha256:" + "b" * 64,
                "release": "harness-v7"}


def open_args(profile="demo", **extra):
    return {"rolloutId": RID, "profile": profile, "stableVersion": 7, "candidateVersion": 8, "behaviorDigest": DIGEST,
            "manifestVersion": "2026-10-13T19:00:00Z/abcdef0", "release": "harness-v8", **extra}


def opened(profile="demo", at=0.0, **extra):
    item = {"state": "IDLE", "revision": 3, "endpoints": {"stable": dict(STABLE_ENTRY)}}
    return ctl.decide(item, "open", open_args(profile, **extra), None, None, at).item


def s(arm, end, error=False, tf=False, selected=WATCH_TOOLS, offered=WATCH_TOOLS, latency=10000.0, cost=0.02,
      cache=0.8):
    return sig.Session(arm, end, error, tf, tuple(offered), frozenset(selected), latency, cost, cache)


def batch(t_from, t_to, n_c, n_s, **kw):
    """n_c candidate and n_s stable sessions ending evenly inside (t_from, t_to]."""
    out = []
    for arm, n, key in (("candidate", n_c, "c"), ("stable", n_s, "s")):
        opts = {k[2:]: v for k, v in kw.items() if k.startswith(key + "_")}
        errors = opts.pop("errors", 0)
        for i in range(n):
            end = t_from + (t_to - t_from) * (i + 1) / (n + 1)
            out.append(s(arm, end, error=i < errors, **opts))
    return out


def tick(item, log, at, profile=None, obs=None):
    return ctl.decide(item, "tick", {}, profile or PROFILES[item["profile"]], log, at, obs)


# ---------------------------------------------------------------- commands
def test_open_keeps_stable_sets_candidate_and_enables_the_schedule():
    d = ctl.decide({"state": "IDLE", "revision": 3, "endpoints": {"stable": dict(STABLE_ENTRY)}}, "open",
                   open_args("prod"), None, None, 100.0)
    item = d.item
    assert item["state"] == "WARMING" and item["canaryPct"] == 5 and item["shadowPct"] == 5
    assert item["endpoints"]["stable"] == STABLE_ENTRY
    assert item["endpoints"]["candidate"] == {"version": 8, "manifestVersion": "2026-10-13T19:00:00Z/abcdef0",
                                              "behaviorDigest": DIGEST, "release": "harness-v8"}
    assert d.kinds() == ["schedule"] and d.actions[0].args == {"enabled": True}
    assert d.transitions == [("IDLE", "WARMING", "open")]
    model = ctl.decide({"state": "ROLLED_BACK", "revision": 1, "endpoints": {}}, "open", open_args("model"), None,
                       None, 0.0).item
    assert model["state"] == "SHADOWING" and model["canaryPct"] == 0 and model["shadowPct"] == 10


def test_open_rejections():
    with pytest.raises(ctl.InvalidTransition, match="invalid_transition"):
        ctl.decide({"state": "IDLE", "endpoints": {"stable": {**STABLE_ENTRY, "version": 6}}}, "open", open_args(),
                   None, None, 0.0)
    with pytest.raises(ctl.InvalidTransition):
        ctl.decide(opened(), "open", open_args(), None, None, 0.0)
    with pytest.raises(ctl.InvalidTransition, match="shadow phase"):
        ctl.decide(None, "open", open_args("prod", experiment=True), None, None, 0.0)
    with pytest.raises(ctl.InvalidTransition, match="rolloutId"):
        ctl.decide(None, "open", {**open_args(), "rolloutId": "r-XYZ"}, None, None, 0.0)


def test_endpoints_replaces_only_named_entries():
    item = {"state": "IDLE", "endpoints": {"stable": dict(STABLE_ENTRY), "candidate": dict(STABLE_ENTRY)}}
    new = {"version": 9, "manifestVersion": "2026-10-14T00:00:00Z/1111111", "behaviorDigest": DIGEST, "release": "r"}
    d = ctl.decide(item, "endpoints", {"candidate": {**new, "extra": "dropped"}}, None, None, 5.0)
    assert d.item["endpoints"] == {"stable": STABLE_ENTRY, "candidate": new}
    with pytest.raises(ctl.InvalidTransition):
        ctl.decide(opened(), "endpoints", {"stable": new}, None, None, 5.0)


def test_reset_abort_and_terminal_ticks():
    with pytest.raises(ctl.InvalidTransition):
        ctl.decide(opened(), "reset", {}, None, None, 0.0)
    done = {**opened(), "state": "ROLLED_BACK"}
    assert ctl.decide(done, "reset", {"reason": "demo prep"}, None, None, 1.0).item["state"] == "IDLE"
    assert ctl.decide(done, "tick", {}, None, None, 1.0).changed is False
    with pytest.raises(ctl.InvalidTransition):
        ctl.decide(done, "abort", {}, None, None, 1.0)
    aborted = ctl.decide(opened(), "abort", {"reason": "operator", "latch": False}, None, None, 2.0)
    assert aborted.item["state"] == "ROLLING_BACK" and aborted.item["latch"] is False
    assert aborted.item["canaryPct"] == 0 and aborted.kinds() == ["retarget", "poll"]
    with pytest.raises(ctl.InvalidTransition):
        ctl.decide(None, "bogus", {}, None, None, 0.0)


# ---------------------------------------------------------------- demo profile ticks
def test_warming_to_observing_evaluates_in_the_same_tick():
    item = opened()
    log = sig.SessionLog(batch(0, 55, 8, 8), item["stepStarts"])
    warming = tick(item, sig.SessionLog(batch(0, 55, 5, 8), item["stepStarts"]), 60.0)
    assert warming.item["state"] == "WARMING"
    d = tick(item, log, 60.0)
    assert [t[:2] for t in d.transitions] == [("WARMING", "OBSERVING")]
    assert d.item["cleanTicks"] == 1 and d.item["lastSignals"]["error_rate"] == "ok"
    assert d.item["observingSince"] == 60.0


def _run_clean(item, minutes, profile=None, per_min=8, start=0):
    log = sig.SessionLog([], item["stepStarts"])
    for m in range(start, start + minutes):
        log.add(batch(m * 60, m * 60 + 55, per_min, per_min))
        d = tick(item, log, m * 60 + 60.0, profile)
        item = d.item
        log.set_steps(item["stepStarts"])
        yield m * 60 + 60.0, d


def test_advancing_needs_three_clean_ticks_and_dwell():
    states = [(t, d.item["state"], d.item["cleanTicks"]) for t, d in _run_clean(opened(), 3)]
    assert states[0][1:] == ("OBSERVING", 1) and states[1][1:] == ("OBSERVING", 2)
    assert states[2][1] == "ADVANCING"


def test_dwell_runs_from_observing_since_not_open():
    """A rollout opened 20 minutes before traffic cannot advance on its first clean tick."""
    profile = {**PROFILES["demo"], "cleanTicksToAdvance": 1}
    item = opened(at=-1200.0)
    states = [(t, d.item["state"]) for t, d in _run_clean(item, 3, profile)]
    assert states[0] == (60.0, "OBSERVING") and states[1] == (120.0, "OBSERVING")
    assert states[2] == (180.0, "ADVANCING")


def _observing(profile="demo"):
    item = opened(profile)
    item["state"] = "OBSERVING"
    return item


def test_two_consecutive_breaches_roll_back():
    item = _observing()
    log = sig.SessionLog([], item["stepStarts"])
    for m in range(2):
        log.add(batch(m * 60, m * 60 + 55, 8, 8, c_errors=4))
        d = tick(item, log, m * 60 + 60.0)
        item = d.item
    assert item["state"] == "ROLLING_BACK" and item["reason"] == "error_rate"
    assert item["canaryPct"] == 0 and item["shadowPct"] == 0 and item["latch"] is True
    assert d.kinds() == ["retarget", "poll"] and d.actions[0].args == {"qualifier": "candidate", "version": 7}


def test_single_error_and_insignificant_differences_never_breach():
    item = _observing()
    one = tick(item, sig.SessionLog(batch(0, 55, 8, 8, c_errors=1), item["stepStarts"]), 60.0)
    assert one.item["breaches"] == 0 and one.item["lastSignals"]["error_rate"] == "ok"
    two = tick(item, sig.SessionLog(batch(0, 55, 8, 8, c_errors=2), item["stepStarts"]), 60.0)
    assert stats.fisher_greater(2, 8, 0, 8) > 0.05
    assert two.item["breaches"] == 0


def test_sliding_tick_without_new_samples_is_inconclusive():
    item = _observing()
    log = sig.SessionLog(batch(0, 55, 8, 8, c_errors=4), item["stepStarts"])
    first = tick(item, log, 60.0).item
    assert first["breaches"] == 1
    again = tick(first, log, 120.0).item          # the window still breaches, but nothing new arrived
    assert again["breaches"] == 1 and again["lastCountedAt"] == 60.0 and again["state"] == "OBSERVING"
    log.add(batch(60, 115, 3, 8, c_errors=3))     # 3 < minNewSamplesPerArm (4) for candidate
    assert tick(first, log, 120.0).item["breaches"] == 1


def test_insufficient_required_signal_makes_the_tick_inconclusive():
    item = _observing()
    item.update(breaches=1, cleanTicks=0)
    sessions = [x if x.arm == "stable" else s("candidate", x.end, offered=()) for x in batch(0, 55, 8, 8)]
    d = tick(item, sig.SessionLog(sessions, item["stepStarts"]), 60.0)
    assert d.item["lastSignals"]["tool_selection"] == "insufficient"
    assert d.item["breaches"] == 1 and d.item["cleanTicks"] == 0 and d.item["lastCountedAt"] is None


def test_insufficient_non_required_signal_does_not_block_a_clean_tick():
    item = _observing()
    d = tick(item, sig.SessionLog(batch(0, 55, 8, 8, c_cache=None, s_cache=None), item["stepStarts"]), 60.0)
    assert d.item["lastSignals"]["latency"] == "insufficient" and d.item["cleanTicks"] == 1


def no_warmup(profile):
    """The profile without the candidate warm-up, for tests about other rules."""
    return {**PROFILES[profile], "candidateWarmupSeconds": 0}


def test_breach_in_a_non_required_signal_counts():
    item = _observing()
    d = tick(item, sig.SessionLog(batch(0, 55, 8, 8, c_latency=40000.0), item["stepStarts"]), 60.0, no_warmup("demo"))
    assert d.item["lastSignals"]["latency"] == "breach" and d.item["breaches"] == 1


def test_warm_up_suppresses_latency_and_cost():
    item = _observing()
    cold = batch(0, 55, 8, 8, c_latency=40000.0, c_cost=0.2, c_cache=0.3)
    d = tick(item, sig.SessionLog(cold, item["stepStarts"]), 60.0)
    assert d.item["lastSignals"]["latency"] == "insufficient" and d.item["lastSignals"]["cost"] == "insufficient"
    assert d.item["breaches"] == 0


def test_candidate_warm_up_delays_only_latency_and_cost():
    """A new runtime version's first sessions wait for cold capacity: latency and cost compare only
    sessions ending CANDIDATE_WARMUP_SECONDS after the step's first candidate session. Tool selection counts at once."""
    assert all(p["candidateWarmupSeconds"] == CANDIDATE_WARMUP_SECONDS == 240 for p in PROFILES.values())
    profile = {**PROFILES["demo"], "cleanTicksToAdvance": 99}   # stay on step 0 throughout
    item = _observing()
    log = sig.SessionLog([], item["stepStarts"])
    for m in range(4):                                     # slow candidate sessions, all inside the warm-up
        log.add(batch(m * 60, m * 60 + 55, 8, 8, c_latency=40000.0, c_cost=0.2))
        item = tick(item, log, m * 60 + 60.0, profile).item
        assert item["lastSignals"]["latency"] == "insufficient" and item["lastSignals"]["cost"] == "insufficient"
        assert item["breaches"] == 0 and item["state"] == "OBSERVING"
    for m in range(4, 7):                                  # warm and normal: the cold sessions are not compared
        log.add(batch(m * 60, m * 60 + 55, 8, 8))
        item = tick(item, log, m * 60 + 60.0, profile).item
        assert item["breaches"] == 0, item["lastSignals"]
    assert item["lastSignals"]["latency"] == "ok"
    slow = _observing()
    log = sig.SessionLog(batch(0, 55, 8, 8), slow["stepStarts"])
    for m in range(4, 6):                                  # a slower candidate after the warm-up is still caught
        log.add(batch(m * 60, m * 60 + 55, 8, 8, c_latency=40000.0))
        slow = tick(slow, log, m * 60 + 60.0, profile).item
    assert slow["state"] == "ROLLING_BACK" and slow["reason"] == "latency"


def test_tool_selection_breaches_during_the_warm_up():
    item = _observing()
    log = sig.SessionLog([], item["stepStarts"])
    for m in range(2):
        log.add(batch(m * 60, m * 60 + 55, 0, 8) + [s("candidate", m * 60 + 10 + i, selected=("query_metrics",))
                                                     for i in range(8)])
        item = tick(item, log, m * 60 + 60.0).item
    assert item["state"] == "ROLLING_BACK" and item["reason"] == "tool_selection"
    assert item["lastSignals"]["latency"] == "insufficient"


def test_bucket_log_first_end():
    from module11.release.rollout_sim import BucketLog
    zero = {"n": 0, "err": 0, "tf": 0, "sel": {t: 0 for t in WATCH_TOOLS}, "lat": [], "cost_s": 0.0, "cost_q": 0.0,
            "q": None}
    some = {**zero, "n": 3, "sel": {t: 3 for t in WATCH_TOOLS}, "lat": [9000.0] * 3}
    log = BucketLog(0.0)
    log.add(60.0, 0, {"candidate": zero, "stable": some})
    log.add(120.0, 0, {"candidate": some, "stable": some})
    log.add(180.0, 1, {"candidate": some, "stable": some})
    assert log.first_end("candidate", 0) == 120.0 and log.first_end("stable", 0) == 60.0
    assert log.first_end("candidate", 1) == 180.0 and log.first_end("candidate", 2) is None


def test_tool_selection_breach():
    item = _observing()
    sessions = batch(0, 55, 0, 8) + [s("candidate", 10 + i, selected=("query_metrics", "get_service_logs"))
                                     for i in range(8)]
    d = tick(item, sig.SessionLog(sessions, item["stepStarts"]), 60.0)
    assert d.item["lastSignals"]["tool_selection"] == "breach"


def test_required_signal_insufficient_for_stall_seconds_emits_stalled_once():
    item = _observing()
    log = sig.SessionLog([], item["stepStarts"])
    emitted = []
    for m in range(12):
        log.add([x if x.arm == "stable" else s("candidate", x.end, offered=())
                 for x in batch(m * 60, m * 60 + 55, 8, 8)])
        d = tick(item, log, m * 60 + 60.0)
        item = d.item
        emitted += [a.args["name"] for a in d.actions if a.kind == "emit"]
    assert emitted == ["m11.rollout.stalled"] and item["stalled"] == ["insufficient:tool_selection"]
    assert item["state"] == "OBSERVING" and item["canaryPct"] == 50


def test_quality_needs_200_per_arm():
    few = {"judge": sig.QualityAgg(199, 0.5, 0.06)}
    many_c, many_s = {"judge": sig.QualityAgg(250, 0.70, 0.0625)}, {"judge": sig.QualityAgg(250, 0.80, 0.0625)}
    assert sig.quality(few, many_s, 0.10).status == "insufficient"
    assert sig.quality(many_c, many_s, 0.10).status == "breach"
    assert sig.quality(many_s, many_s, 0.10).status == "ok"


# ---------------------------------------------------------------- batch profiles
def _prod_observing(step=0, starts=(0.0,), **fields):
    item = _observing("prod")
    item.update(stepIndex=step, stepStarts=list(starts), canaryPct=PROFILES["prod"]["steps"][step], **fields)
    return item


def test_batch_ticks_never_share_a_session():
    item = _prod_observing()
    log = sig.SessionLog(batch(0, 55, 50, 50), item["stepStarts"])
    first = tick(item, log, 60.0, no_warmup("prod")).item
    assert first["lastCountedAt"] == 60.0
    log.add(batch(60, 115, 30, 30))
    second = tick(first, log, 120.0, no_warmup("prod")).item      # 30 new per arm: the batch keeps growing
    assert second["lastCountedAt"] == 60.0 and second["cleanTicks"] == first["cleanTicks"]
    log.add(batch(120, 175, 20, 20))
    third = tick(second, log, 180.0, no_warmup("prod")).item
    assert third["lastCountedAt"] == 180.0
    assert log.count("candidate", 0, 60.0, 180.0) == 50   # the second batch is exactly the sessions after 60 s


def test_rate_rule_exact_fixtures():
    assert f"{stats.stratified_exact_greater([(2, 60, 22, 1140), (10, 150, 2, 450)]):.4e}" == "2.1021e-04"
    assert f"{stats.stratified_exact_greater([(3, 60, 0, 1140)]):.4e}" == "1.1912e-04"
    base = [(3, 60, 0, 1140)]
    assert stats.stratified_exact_greater(base + [(0, 50, 0, 50), (0, 0, 4, 40)]) == stats.stratified_exact_greater(base)


def test_rate_rule_rd_mh_not_crude_rates():
    strata = [(1, 60, 11, 1140), (23, 450, 23, 450)]
    assert stats.rd_mh(strata) == pytest.approx(0.0014, abs=5e-5)
    assert sig.pooled_rate("error_rate", strata, 0.02 / 270, 3).status == "ok"


def test_rate_rule_skips_a_look_with_fewer_than_five_new_candidates():
    item = _prod_observing()
    log = sig.SessionLog(batch(0, 55, 53, 50), item["stepStarts"])
    item["lastLookCandidates"] = 50
    d = tick(item, log, 60.0)
    assert d.item["looks"] == 0 and d.item["lookedStep"] is None


def test_rate_rule_breaching_look_zeroes_clean_ticks_and_survives_a_clean_batch():
    item = _prod_observing(cleanTicks=2)
    log = sig.SessionLog(batch(0, 55, 50, 500, c_errors=10), item["stepStarts"])
    first = tick(item, log, 60.0).item
    assert first["rateBreaches"] == 1 and first["cleanTicks"] == 0 and first["lastLookBreached"]
    log.add(batch(60, 115, 50, 500))                     # a clean batch; the pooled look still breaches
    d = tick(first, log, 120.0)
    assert d.item["breaches"] == 0
    assert d.item["state"] == "ROLLING_BACK" and d.item["reason"] == "error_rate"


def test_rate_rule_non_breaching_look_resets_the_counter():
    item = _prod_observing(rateBreaches=1)
    d = tick(item, sig.SessionLog(batch(0, 55, 50, 500), item["stepStarts"]), 60.0)
    assert d.item["looks"] == 1 and d.item["rateBreaches"] == 0 and d.item["state"] == "OBSERVING"


def test_rate_rule_look_budget_ends_in_rate_looks_exhausted():
    item = _prod_observing(looks=270)
    d = tick(item, sig.SessionLog(batch(0, 55, 50, 500, c_errors=10), item["stepStarts"]), 60.0)
    assert d.item["state"] == "OBSERVING" and d.item["looks"] == 270 and d.item["rateExhausted"]
    assert [a.args for a in d.actions if a.kind == "emit"] == [
        {"name": "m11.rollout.stalled", "detail": {"reason": "rate_looks_exhausted"}}]
    assert not d.transitions


def test_quality_hold_stall():
    """A prod rollout held at its last canary step with insufficient quality stalls after stallSeconds,
    measured from the first tick on which it could otherwise advance; it neither advances nor rolls back."""
    starts = [0.0, 3000.0, 6000.0]
    item = _prod_observing(step=2, starts=starts, observingSince=6060.0, cleanTicks=2)
    log = sig.SessionLog([], starts)
    t0 = 6000.0 + 1800 + 60
    emitted = []
    for m in range(0, 14400 // 60 + 3):
        t = t0 + m * 60
        log.add(batch(t - 60, t - 5, 50, 50))
        d = tick(item, log, t, no_warmup("prod"))
        item = d.item
        emitted += [a.args["detail"]["reason"] for a in d.actions if a.kind == "emit"]
        assert item["state"] == "OBSERVING" and item["canaryPct"] == 50
    assert item["holdSince"] == t0
    assert emitted == ["quality_hold"]


# ---------------------------------------------------------------- shadow phase
def _shadowing(**extra):
    return opened("model", **extra)


def _deltas(mean, n=250):
    import random
    rnd = random.Random(7)
    return {"Builtin.GoalSuccessRate": [mean + rnd.gauss(0, 0.2) for _ in range(n)]}


def test_shadow_verdict_start_poll_and_outcomes():
    item = _shadowing()
    early = ctl.decide(item, "tick", {}, None, None, 3600.0, {"shadowPairs": [("c1", "s1")]})
    assert early.item["state"] == "SHADOWING" and not early.actions
    pairs = [(f"c{i}", f"s{i}") for i in range(600)]
    start = ctl.decide(item, "tick", {}, None, None, 24 * 3600.0, {"shadowPairs": pairs, "stagekey": "prod"})
    action = start.actions[0]
    assert action.kind == "start_batch" and len(action.args["sessionIds"]["c"]) == 500
    assert action.args["names"] == {"c": "m11_prod_shadow_0123456789abcdef_c1", "s": "m11_prod_shadow_0123456789abcdef_s1"}
    running = start.item
    poll = ctl.decide(running, "tick", {}, None, None, 24 * 3600 + 60.0, {"batches": {"c": {"status": "IN_PROGRESS"}}})
    assert poll.item["state"] == "SHADOWING" and poll.changed is False
    done = {"batches": {"c": {"status": "COMPLETED"}, "s": {"status": "COMPLETED_WITH_ERRORS"}}}
    fail = ctl.decide(running, "tick", {}, None, None, 25 * 3600.0, {**done, "shadowDeltas": _deltas(-0.10)})
    assert fail.item["state"] == "ROLLING_BACK" and fail.item["reason"] == "shadow_regression" and fail.item["latch"]
    ok = ctl.decide(running, "tick", {}, None, None, 25 * 3600.0, {**done, "shadowDeltas": _deltas(0.0)})
    assert ok.item["state"] == "WARMING" and ok.item["canaryPct"] == 5


def test_shadow_retries_two_hours_later_on_few_pairs_or_a_failed_batch():
    started = ctl.decide(_shadowing(), "shadow-evaluate", {}, None, None, 3600.0, {"shadowPairs": [("c", "s")]}).item
    assert started["shadow"]["running"]
    again = ctl.decide(started, "shadow-evaluate", {}, None, None, 3700.0, {})
    assert again.changed is False and again.note == "verdict already started"
    done = {"batches": {"c": {"status": "COMPLETED"}, "s": {"status": "COMPLETED"}}}
    few = ctl.decide(started, "tick", {}, None, None, 4000.0, {**done, "shadowDeltas": _deltas(0.0, n=150)}).item
    assert few["state"] == "SHADOWING" and few["shadow"]["nextAttemptAt"] == 4000.0 + 7200
    failed = ctl.decide(started, "tick", {}, None, None, 4000.0, {"batches": {"c": {"status": "FAILED"}}}).item
    assert failed["shadow"]["nextAttemptAt"] == 4000.0 + 7200
    late = {**failed, "openedAt": -24 * 3600.0}
    assert not ctl.decide(late, "tick", {}, None, None, 5000.0, {"shadowPairs": []}).actions
    second = ctl.decide(late, "tick", {}, None, None, 11300.0, {"shadowPairs": [("c", "s")], "stagekey": "prod"})
    assert second.actions[0].args["names"]["c"].endswith("_c2")


def test_shadow_timeout_rolls_back_without_a_latch():
    d = ctl.decide(_shadowing(), "tick", {}, None, None, 30 * 3600.0, {})
    assert d.item["state"] == "ROLLING_BACK" and d.item["reason"] == "shadow_timeout" and d.item["latch"] is False
    assert {"name": "m11.rollout.shadow_timeout", "detail": {}} in [a.args for a in d.actions if a.kind == "emit"]


def test_experiment_records_the_verdict_and_stays_shadowing():
    started = ctl.decide(_shadowing(experiment=True), "shadow-evaluate", {}, None, None, 7200.0, {"shadowPairs": []}).item
    done = {"batches": {"c": {"status": "COMPLETED"}, "s": {"status": "COMPLETED"}}, "shadowDeltas": _deltas(-0.2)}
    d = ctl.decide(started, "tick", {}, None, None, 9000.0, done)
    assert d.item["state"] == "SHADOWING" and d.item["canaryPct"] == 0 and d.item["shadow"]["verdict"] == "fail"
    assert d.kinds() == ["audit"]


def test_operator_shadow_pass_and_fail():
    assert ctl.decide(_shadowing(), "shadow-pass", {}, None, None, 1.0).item["state"] == "WARMING"
    failed = ctl.decide(_shadowing(), "shadow-fail", {"reason": "operator"}, None, None, 1.0).item
    assert failed["state"] == "ROLLING_BACK" and failed["reason"] == "operator"
    with pytest.raises(ctl.InvalidTransition):
        ctl.decide(opened(), "shadow-pass", {}, None, None, 1.0)


def test_batch_evaluation_names_and_rollout_ids():
    pattern = re.compile(ctl.BATCH_NAME)
    for stagekey in ("prod", "dev"):
        for attempt in range(1, 100):
            for arm in ("c", "s"):
                assert pattern.match(ctl.batch_name(stagekey, RID, arm, attempt))
    assert len(ctl.batch_name("prod", RID, "c", 99)) == 36
    with pytest.raises(ValueError):
        ctl.batch_name("prod", "r-ABC", "c", 1)


# ---------------------------------------------------------------- orchestration with fake ports
class FakePorts:
    def __init__(self, item=None, ready_after=25.0, endpoint_status=None, fail_ledger=False, conflicts=0):
        self.t = 1000.0
        self.item = copy.deepcopy(item)
        self.calls = []
        self.params = {"/m11/devops-companion/prod/candidate": json.dumps(
            {"manifestVersion": "mv8", "deployment": {"prod": {"endpoints": {"stable": 7, "candidate": 8}}}})}
        self.ready_after, self.endpoint_status = ready_after, endpoint_status
        self.issued = {}
        self.fail_ledger, self.conflicts = fail_ledger, conflicts
        self.ledger_attempts = 0
        self.logs, self.metrics = [], []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.t += seconds

    def get_item(self):
        self.calls.append(("get_item",))
        return copy.deepcopy(self.item)

    def put_item(self, item, expected):
        if self.conflicts:
            self.conflicts -= 1
            self.calls.append(("put_item_conflict",))
            raise ctl.ConditionalCheckFailed()
        self.calls.append(("put_item", item["state"], item.get("canaryPct"), expected))
        self.item = copy.deepcopy(item)

    def put_audit(self, event):
        self.calls.append(("audit", event["decision"]))

    def log_for(self, item):
        return sig.SessionLog([], item["stepStarts"])

    def shadow_obs(self, item):
        return {}

    def get_endpoint(self, qualifier):
        if self.endpoint_status:
            return {"status": self.endpoint_status, "liveVersion": 8}
        version, at = self.issued.get(qualifier, (None, None))
        if at is not None and self.t - at >= self.ready_after:
            return {"status": "READY", "liveVersion": version}
        return {"status": "UPDATING", "liveVersion": 8 if qualifier == "candidate" else 7, "targetVersion": version}

    def update_endpoint(self, qualifier, version):
        self.calls.append(("update_endpoint", qualifier, version))
        self.issued[qualifier] = (version, self.t)

    def get_schedule(self):
        return {"Name": "m11-prod-controller", "GroupName": "default", "ScheduleExpression": "cron(* * * * ? *)",
                "Target": {"Arn": "arn", "RoleArn": "role"}, "FlexibleTimeWindow": {"Mode": "OFF"},
                "State": "DISABLED", "Arn": "arn:schedule", "CreationDate": "x", "Description": "tick"}

    def update_schedule(self, **fields):
        self.calls.append(("update_schedule", fields))

    def get_param(self, name):
        return self.params.get(name)

    def put_param(self, name, value):
        self.calls.append(("put_param", name))
        self.params[name] = value

    def put_ledger(self, key, body):
        self.ledger_attempts += 1
        self.calls.append(("put_ledger",))
        if self.fail_ledger:
            raise OSError("s3 down")

    def put_metric(self, namespace, name, value):
        self.metrics.append((namespace, name, value))

    def emit(self, name, detail):
        self.calls.append(("emit", name))

    def alarm(self, name, reason=None):
        self.calls.append(("alarm", name))

    def start_batch(self, name, session_ids):
        self.calls.append(("start_batch", name))
        return f"be-{name}"

    def log(self, level, event, **fields):
        self.logs.append((level, event, fields))


def controller(ports):
    return ctl.Controller(ports, "prod", "/m11/devops-companion/prod/current", "/m11/devops-companion/prod/candidate")


def _observing_item():
    item = opened("prod", at=0.0)
    item["state"] = "OBSERVING"
    return item


def test_rolling_back_writes_canary_zero_first_and_finishes_in_the_same_invocation():
    ports = FakePorts(_observing_item(), ready_after=25.0)
    result = controller(ports).handle({"command": "abort", "args": {"reason": "error_rate"}})
    assert result["state"] == "ROLLED_BACK"
    kinds = [c[0] for c in ports.calls]
    first_put = kinds.index("put_item")
    assert ports.calls[first_put][1:3] == ("ROLLING_BACK", 0)
    assert first_put < kinds.index("update_endpoint")
    assert [c for c in ports.calls if c[0] == "update_endpoint"] == [("update_endpoint", "candidate", 7)]
    latch_put = next(c for c in ports.calls if c[0] == "put_param" and "/latch/" in c[1])
    assert latch_put[1] == "/m11/devops-companion/latch/" + "a" * 64
    assert json.loads(ports.params["/m11/devops-companion/prod/candidate"]) == {"state": "none"}
    assert ports.item["endpoints"]["candidate"] == STABLE_ENTRY
    assert ("emit", "m11.rollout.rolled_back") in ports.calls
    sched = next(c for c in ports.calls if c[0] == "update_schedule")[1]
    assert sched["State"] == "DISABLED" and "Arn" not in sched and sched["Description"] == "tick"
    assert kinds.index("put_ledger") > kinds.index("emit")


def test_ready_after_55_seconds_finishes_on_the_next_tick():
    ports = FakePorts(_observing_item(), ready_after=55.0)
    c = controller(ports)
    assert c.handle({"command": "abort", "args": {"reason": "x"}})["state"] == "ROLLING_BACK"
    ports.t += 20
    assert c.handle({"command": "tick"})["state"] == "ROLLED_BACK"


def test_update_failed_is_reissued_at_most_three_times_and_stuck_alarms_at_ten_minutes():
    ports = FakePorts(_observing_item(), endpoint_status="UPDATE_FAILED")
    c = controller(ports)
    c.handle({"command": "abort", "args": {"reason": "x"}})
    assert sum(1 for x in ports.calls if x[0] == "update_endpoint") == 4       # 1 + 3 re-issues
    ports.t += 600
    c.handle({"command": "tick"})
    c.handle({"command": "tick"})
    assert [x for x in ports.calls if x[0] == "alarm"] == [("alarm", "m11-rollback-stuck")]


def test_promoted_rewrites_current_with_live_endpoints():
    item = _observing_item()
    item.update(state="ADVANCING", stepIndex=3, canaryPct=50)
    ports = FakePorts(item)
    ports.issued["stable"] = (8, 0.0)
    result = controller(ports).handle({"command": "tick"})
    assert result["state"] == "PROMOTED"
    current = json.loads(ports.params["/m11/devops-companion/prod/current"])
    assert current["deployment"]["prod"]["endpoints"] == {"stable": 8, "candidate": 8}
    assert json.loads(ports.params["/m11/devops-companion/prod/candidate"]) == {"state": "none"}
    assert ports.item["endpoints"]["stable"] == ports.item["endpoints"]["candidate"]
    assert ports.item["canaryPct"] == 0 and ("emit", "m11.rollout.promoted") in ports.calls


def test_open_enables_the_schedule_with_every_field():
    ports = FakePorts({"state": "IDLE", "revision": 4, "endpoints": {"stable": dict(STABLE_ENTRY)}})
    result = controller(ports).handle({"command": "open", "args": open_args("prod")})
    assert result["ok"] and result["state"] == "WARMING" and result["revision"] == 5
    fields = next(c for c in ports.calls if c[0] == "update_schedule")[1]
    assert fields == {"Name": "m11-prod-controller", "GroupName": "default", "ScheduleExpression": "cron(* * * * ? *)",
                      "Target": {"Arn": "arn", "RoleArn": "role"}, "FlexibleTimeWindow": {"Mode": "OFF"},
                      "Description": "tick", "State": "ENABLED"}
    assert ("audit", "IDLE->WARMING") in ports.calls


def test_conditional_conflict_rereads_once_then_gives_up():
    ports = FakePorts(_observing_item(), conflicts=1)
    assert controller(ports).handle({"command": "abort", "args": {"reason": "x"}})["ok"]
    assert sum(1 for x in ports.calls if x[0] == "get_item") == 2
    ports = FakePorts(_observing_item(), conflicts=2)
    assert controller(ports).handle({"command": "abort", "args": {}}) == {"ok": False, "error": "conflict"}
    assert ports.logs[-1][:2] == ("WARN", "conditional_check_failed")
    bad = controller(FakePorts(_observing_item())).handle({"command": "reset"})
    assert bad["error"] == "invalid_transition"


def test_ledger_failure_never_blocks_the_rollback():
    ok, failing = FakePorts(_observing_item()), FakePorts(_observing_item(), fail_ledger=True)
    for ports in (ok, failing):
        assert controller(ports).handle({"command": "abort", "args": {"reason": "error_rate"}})["state"] == "ROLLED_BACK"
    strip = lambda calls: [c[0] for c in calls if c[0] not in ("put_ledger", "sleep")]  # noqa: E731
    assert strip(ok.calls) == strip(failing.calls)
    assert failing.ledger_attempts == 4
    assert [l[:2] for l in failing.logs] == [("ERROR", "ledger_write_failed")]
    assert failing.logs[0][2]["entry"]["kind"] == "latch"
    assert failing.metrics == [("m11", "LedgerWriteFailures", 1.0)]
    assert [round(c[1]) for c in failing.calls if c[0] == "sleep"][-3:] in ([1, 2, 4], [1, 2, 5], [1, 2, 3],
                                                                           [1, 3, 4], [1, 3, 5], [1, 3, 3])


def test_shadow_batches_are_started_ledgered_and_stored():
    item = opened("model", at=0.0)
    ports = FakePorts(item)
    ports.t = 24 * 3600.0
    ports.shadow_obs = lambda it: {"shadowPairs": [("c1", "s1")]}
    controller(ports).handle({"command": "tick"})
    assert [c[1] for c in ports.calls if c[0] == "start_batch"] == ["m11_prod_shadow_0123456789abcdef_c1",
                                                                    "m11_prod_shadow_0123456789abcdef_s1"]
    assert ports.item["shadow"]["batchIds"] == {"c": "be-m11_prod_shadow_0123456789abcdef_c1",
                                                "s": "be-m11_prod_shadow_0123456789abcdef_s1"}
    assert ports.ledger_attempts == 2


# ---------------------------------------------------------------- simulator
def _sizes(names):
    return {n: (min(sim.case(n).runs, int(SIM_RUNS)) if SIM_RUNS else sim.case(n).runs) for n in names}


def test_demo_profile_power():
    """S7 shape on the session-duration fixture: P(ROLLED_BACK <= t0+4:30) >= 0.90, <= t0+5:00 >= 0.99,
    P(PROMOTED) <= 0.001."""
    row = sim.run(["demo-power-nominal"], ROOT, runs=_sizes(["demo-power-nominal"]))[0]
    assert row["threshold"] == sim.DEMO_POWER
    if row["runs"] < sim.case("demo-power-nominal").runs:
        # Reduced runs (M11_SIM_RUNS, the MR `validate` job and Analyze): the demo bar, P >= 0.95 for B5 (a
        # recorded session with re-takes). The full-size grid, `sim verify` and the committed report keep 0.99.
        assert row["rolledBackBy430"] >= 0.90 and row["rolledBackBy500"] >= 0.95
        assert row["promoted"] <= max(1, int(0.001 * row["runs"]))
        return
    assert row["rolledBackBy430"] >= 0.90 and row["rolledBackBy500"] >= 0.99
    assert row["promoted"] / row["runs"] <= 0.001 and row["passed"]


def _armed_then_canary(open_at: float) -> tuple[list, list]:
    """A demo rollout opened at `open_at` (<= 0), ticked every minute with no traffic until t0 = 0, then a bad
    canary (candidate payments sessions skip search_runbooks 7 times in 8; stable always calls it) with READY
    observed after the retarget. Returns (actions during the armed wait, [(t, state)] after t0)."""
    item = opened(at=open_at)
    log = sig.SessionLog([], item["stepStarts"])
    waiting, t = [], open_at
    while t + 60.0 <= 0.0:
        t += 60.0
        d = tick(item, log, t)
        assert d.item["state"] == "WARMING" and d.item["canaryPct"] == 50
        waiting += [(a.kind, a.args.get("name")) for a in d.actions]
        item = d.item
    skip = tuple(x for x in WATCH_TOOLS if x != "search_runbooks")
    timeline = []
    for m in range(6):
        log.add([s("stable", m * 60 + 5 + i * 6) for i in range(8)]
                + [s("candidate", m * 60 + 5 + i * 6, selected=WATCH_TOOLS if i == 0 else skip) for i in range(8)])
        now = m * 60 + 60.0
        obs = {"endpoints": {"candidate": {"status": "READY", "liveVersion": 7}}} \
            if item["state"] == "ROLLING_BACK" else None
        item = tick(item, log, now, obs=obs).item
        log.set_steps(item["stepStarts"])
        timeline.append((now, item["state"]))
        if item["state"] == "ROLLED_BACK":
            break
    return waiting, timeline


@pytest.mark.parametrize("wait_minutes", [30, 120])
def test_armed_wait(wait_minutes):
    """A demo rollout opened 30 or 120 minutes before traffic (the armed wait, B0-B4 re-takes)
    changes nothing but the stall event before traffic, and then rolls back exactly as one opened at t0 does,
    inside test_demo_profile_power's bound (ROLLED_BACK <= t0 + 4:30)."""
    waiting, timeline = _armed_then_canary(-60.0 * wait_minutes)
    assert set(waiting) <= {("emit", "m11.rollout.stalled"), ("alarm", None)}
    _, reference = _armed_then_canary(0.0)
    assert timeline == reference
    rolled_back = [t for t, state in timeline if state == "ROLLED_BACK"]
    assert rolled_back and rolled_back[0] <= 270.0


def test_null_effect_false_rollback_demo():
    names = ["demo-null-s7", "demo-null-rate20"]
    for row in sim.run(names, ROOT, runs=_sizes(names)):
        assert row["pRolledBack"] <= 0.05, row


SLOW_NULL = [c.name for c in sim.CASES if c.kind == "batch" and not c.effect]
SLOW_POWER = [c.name for c in sim.CASES if c.kind == "batch" and c.effect]


def _compare_with_report(rows):
    report = {r["case"]: r for r in (sim.read_report(ROOT) or {}).get("rows", [])}
    for row in rows:
        stored = report.get(row["case"])
        if stored and stored["runs"] == row["runs"]:
            assert stored == row, f"{row['case']}: the grid differs from the committed report"


@pytest.mark.skipif(not SLOW, reason="slow grid: set M11_SLOW_TESTS=1")
def test_null_effect_false_rollback():
    rows = sim.run(SLOW_NULL, ROOT, runs=_sizes(SLOW_NULL))
    assert len(rows) == 18
    for row in rows:
        assert row["cpUpper"] <= 0.01 and row["stalled"] == 0, row
    _compare_with_report(rows)


@pytest.mark.skipif(not SLOW, reason="slow grid: set M11_SLOW_TESTS=1")
def test_prod_profile_power():
    rows = sim.run(SLOW_POWER, ROOT, runs=_sizes(SLOW_POWER))
    assert len(rows) == 6
    for row in rows:
        assert row["power"] >= 0.80, row
    _compare_with_report(rows)


def test_controller_sim_report_current():
    """The committed report's closureSha256 equals the simulator closure's hash, it passed, and every row
    carries ACCEPTANCE's threshold and meets it."""
    assert sim.report_problems(ROOT) == []


def test_sim_closure_covers_the_controller():
    from module11.release.closure import closure
    files = closure(sim.SIM_ENTRY, ROOT)
    for name in ("controller.py", "signals.py", "stats.py", "router_spec.py", "__init__.py"):
        assert f"module11/release/{name}" in files
    assert "module11/__init__.py" in files


def test_report_problems_catch_a_loosened_threshold(tmp_path):
    doc = sim.read_report(ROOT)
    assert doc is not None
    bad = copy.deepcopy(doc)
    bad["rows"][0]["threshold"] = {"maxRolledBack": 0.5}
    assert any("threshold differs" in p for p in sim.report_problems(ROOT, bad))
    bad = copy.deepcopy(doc)
    bad["closureSha256"] = "sha256:" + "0" * 64
    assert any("closureSha256" in p for p in sim.report_problems(ROOT, bad))
