# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Statistics for the gate and the controller."""
from fractions import Fraction
from itertools import product
from math import comb, isclose

import pytest

from module11.release import stats as S


def test_mean_sd_p95():
    values = [0.8, 0.9, 1.0, 0.7]
    assert S.mean(values) == pytest.approx(0.85)
    assert S.sd(values) == pytest.approx(0.1290994, rel=1e-6)
    assert S.sd([0.5]) == 0.0
    assert S.p95(list(range(1, 101))) == 95
    assert S.p95([3.0]) == 3.0
    with pytest.raises(ValueError):
        S.mean([])


def test_noise_floor_from_run_means_and_scaling():
    run_means = [0.86, 0.88, 0.84, 0.87, 0.85, 0.89, 0.86, 0.87]
    assert S.noise_floor(run_means) == pytest.approx(S.sd(run_means))
    assert S.scaled_noise(0.02, 3, 3) == pytest.approx(0.02)
    assert S.scaled_noise(0.02, 3, 2) == pytest.approx(0.02 * (3 / 2) ** 0.5)
    with pytest.raises(ValueError):
        S.scaled_noise(0.02, 3, 0)


def _brute_force(strata):
    """P(sum A_k >= observed) by enumerating every stratum's hypergeometric exactly."""
    supports = []
    for a, n1, c, n2 in strata:
        k = a + c
        total = comb(n1 + n2, n1)
        supports.append([(x, Fraction(comb(k, x) * comb(n1 + n2 - k, n1 - x), total))
                         for x in range(max(0, k - n2), min(k, n1) + 1)])
    observed = sum(s[0] for s in strata)
    tail = Fraction(0)
    for combo in product(*supports):
        if sum(x for x, _ in combo) >= observed:
            p = Fraction(1)
            for _, q in combo:
                p *= q
            tail += p
    return float(tail)


def test_two_strata_exact_p_matches_hand_computed_value():
    strata = [(2, 60, 22, 1140), (10, 150, 2, 450)]
    p = S.stratified_exact_greater(strata)
    assert f"{p:.4e}" == "2.1021e-04"
    assert isclose(p, _brute_force(strata), rel_tol=1e-10)


def test_one_stratum_equals_fisher():
    p = S.stratified_exact_greater([(3, 60, 0, 1140)])
    assert f"{p:.4e}" == "1.1912e-04"
    assert p == S.fisher_greater(3, 60, 0, 1140)
    assert isclose(p, _brute_force([(3, 60, 0, 1140)]), rel_tol=1e-10)


def test_degenerate_strata_leave_p_unchanged():
    base = [(2, 60, 22, 1140), (10, 150, 2, 450)]
    p = S.stratified_exact_greater(base)
    for extra in [(0, 40, 0, 400), (0, 0, 3, 30), (2, 20, 0, 0)]:
        assert isclose(S.stratified_exact_greater(base + [extra]), p, rel_tol=1e-12)


def test_rd_mh_is_not_the_crude_difference():
    strata = [(1, 60, 11, 1140), (23, 450, 23, 450)]
    crude = (1 + 23) / (60 + 450) - (11 + 23) / (1140 + 450)
    assert crude > 0.025
    assert S.rd_mh(strata) == pytest.approx(0.0014, abs=5e-5)


def test_fisher_two_sided_and_binomial():
    assert S.fisher_two_sided(5, 13, 12, 13) == pytest.approx(0.01117, rel=1e-3)
    assert S.fisher_two_sided(0, 10, 0, 10) == 1.0
    assert S.binom_greater(6, 50, 0.05) == pytest.approx(
        sum(comb(50, k) * 0.05 ** k * 0.95 ** (50 - k) for k in range(6, 51)), rel=1e-9)
    assert S.binom_greater(0, 10, 0.5) == 1.0


def test_clopper_pearson_upper():
    assert S.clopper_pearson_upper(5, 1000) == pytest.approx(0.01163, abs=2e-5)    # "about 0.012"
    assert S.clopper_pearson_upper(4, 4000) == pytest.approx(0.0026, abs=5e-5)     # the 0.0010 (0.0026) cell
    assert S.clopper_pearson_upper(0, 4000) == pytest.approx(0.000922, abs=1e-5)


def test_welch_and_paired_t():
    assert S.welch_less(0.70, 0.0625, 200, 0.80, 0.0625, 200) < 1e-4
    assert S.welch_less(0.80, 0.0625, 200, 0.80, 0.0625, 200) == pytest.approx(0.5)
    assert S.welch_greater(1.3, 0.04, 50, 1.0, 0.04, 50) < 1e-6
    assert S.t_cdf(1.96, 1e7) == pytest.approx(0.975, abs=1e-4)
    assert S.paired_less([-0.1] * 5 + [-0.12] * 5) == 0.0 or S.paired_less([-0.1] * 5 + [-0.12] * 5) < 1e-6
    assert S.paired_less([0.01, -0.01, 0.02, -0.02]) > 0.3


# ---------------------------------------------------------------- exceedance test (latency signal)
def test_exceedance_distribution_is_exact():
    for n_c, n_s, m in [(1, 1, 1), (1, 2, 2), (8, 8, 8), (12, 20, 19), (30, 25, 24)]:
        pmf = [S.exceedance_greater(j, n_c, n_s, m) - S.exceedance_greater(j + 1, n_c, n_s, m)
               for j in range(n_c + 1)]
        assert abs(sum(pmf) - 1.0) < 1e-9
        for j, value in enumerate(pmf):     # the closed form
            want = comb(m - 1 + n_c - j, n_c - j) * comb(n_s - m + j, j) / comb(n_s + n_c, n_c)
            assert abs(value - want) < 1e-9
    assert abs(S.exceedance_greater(1, 1, 2, 2) - 1 / 3) < 1e-12        # a new value above the max of 2
    assert abs(S.exceedance_greater(4, 8, 8, 8) - 495 / 12870) < 1e-12  # 4 of 8 above the max of 8
    with pytest.raises(ValueError):
        S.exceedance_greater(1, 5, 5, 0)


def _arm(latencies):
    from module11.release import signals as sig
    latencies = list(latencies)
    return sig.ArmAgg(n=len(latencies), latencies=list(latencies), cache_n=len(latencies),
                      cache_sum=0.8 * len(latencies))


def test_latency_signal_keeps_its_false_breach_rate_on_skewed_small_windows():
    """Both arms from one right-skewed distribution (lognormal, like measured session durations): the per-look
    false breach rate stays at or under alpha at the demo profile's 8-20 sessions per arm. The old binomial test
    against a fixed 5% breached 0.098 of these looks (0.036 now)."""
    import random
    from module11.release import signals as sig
    rnd = random.Random(7)
    looks = breaches = 0
    for _ in range(4000):
        n_c, n_s = rnd.randint(8, 20), rnd.randint(8, 20)
        c = _arm(rnd.lognormvariate(9.4, 0.45) for _ in range(n_c))
        s = _arm(rnd.lognormvariate(9.4, 0.45) for _ in range(n_s))
        looks += 1
        breaches += sig.latency(c, s, 0.05, 8).status == sig.BREACH
    assert breaches / looks <= 0.05, breaches / looks


def test_latency_signal_still_catches_a_slower_candidate():
    import random
    from module11.release import signals as sig
    rnd = random.Random(11)
    caught = sum(sig.latency(_arm(2.5 * rnd.lognormvariate(9.4, 0.45) for _ in range(15)),
                             _arm(rnd.lognormvariate(9.4, 0.45) for _ in range(15)), 0.05, 8).status == sig.BREACH
                 for _ in range(500))
    assert caught / 500 >= 0.8, caught / 500
