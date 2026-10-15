# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Statistics for the gate engine and the rollback controller. Pure.

Exact tests only for counts: Fisher (one- and two-sided), the stratified exact test that
pools a rollout's exposure steps (`stratified_exact_greater`), the binomial tail, and the
distribution-free exceedance test the latency signal uses (`exceedance_greater`).
Welch's and the paired t-test use the Student t distribution through the regularized
incomplete beta function, so no SciPy is needed. Clopper-Pearson bounds use its inverse.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

Stratum = tuple[int, int, int, int]   # (candidate events, candidate n, stable events, stable n)


# ---------------------------------------------------------------- descriptive
def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean of no values")
    return math.fsum(values) / len(values)


def sd(values: Sequence[float]) -> float:
    """Sample standard deviation (n - 1); 0.0 for a single value."""
    n = len(values)
    if n == 0:
        raise ValueError("sd of no values")
    if n == 1:
        return 0.0
    m = mean(values)
    return math.sqrt(math.fsum((v - m) ** 2 for v in values) / (n - 1))


def p95(values: Sequence[float]) -> float:
    return percentile(values, 95.0)


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least q% of the sample at or below it."""
    if len(values) == 0:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return float(ordered[rank - 1])


def noise_floor(run_means: Sequence[float]) -> float:
    """The noise floor of one (evaluator, category, scope): the SD of the calibration run means."""
    return sd(run_means)


def scaled_noise(noise_sd: float, r_cal: int, r: int) -> float:
    """noise_r = noise x sqrt(r_cal / r)."""
    if r <= 0 or r_cal <= 0:
        raise ValueError("repeat counts must be positive")
    return noise_sd * math.sqrt(r_cal / r)


# ---------------------------------------------------------------- special functions
_LOGFACT = np.zeros(1, dtype=np.float64)


def _logfact(upto: int) -> np.ndarray:
    """log(k!) for k = 0..upto, from math.lgamma (accurate to ~1e-15 relative), grown on demand."""
    global _LOGFACT
    if upto >= len(_LOGFACT):
        size = max(upto + 1, 2 * len(_LOGFACT))
        _LOGFACT = np.array([math.lgamma(k + 1.0) for k in range(size)], dtype=np.float64)
    return _LOGFACT


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    """Inverse of I_x(a, b) in x, by bisection (60 halvings: below 1e-17)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def t_cdf(t: float, df: float) -> float:
    """Student t CDF."""
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


# ---------------------------------------------------------------- count tests
def _hypergeom_pmf(events: int, n1: int, n2: int) -> tuple[int, np.ndarray]:
    """(lowest support value, pmf) of the candidate-arm event count given the stratum margins."""
    total = n1 + n2
    lo, hi = max(0, events - n2), min(events, n1)
    lf = _logfact(total)
    x = np.arange(lo, hi + 1)
    logp = (lf[events] - lf[x] - lf[events - x]
            + lf[total - events] - lf[n1 - x] - lf[n2 - events + x]
            - (lf[total] - lf[n1] - lf[n2]))
    pmf = np.exp(logp)
    # Trim the underflowed ends; they hold no probability mass at double precision.
    nz = np.nonzero(pmf)[0]
    if len(nz) == 0:
        return lo, np.array([1.0])
    return lo + int(nz[0]), pmf[nz[0]:nz[-1] + 1]


def stratified_exact_greater(strata: Iterable[Stratum]) -> float:
    """P(sum A_k >= sum a_k | every stratum's margins), common odds ratio 1.

    The null distribution is the convolution of the per-stratum hypergeometric pmfs. A stratum
    with no events or an empty arm is fixed by its margins and only shifts the sum. With one
    stratum this is the one-sided Fisher exact test."""
    observed = 0
    offset = 0
    dist = np.array([1.0])
    for a, n1, c, n2 in strata:
        if min(a, n1, c, n2) < 0 or a > n1 or c > n2:
            raise ValueError(f"invalid stratum {(a, n1, c, n2)}")
        observed += a
        events = a + c
        if events == 0 or n1 == 0 or n2 == 0:
            offset += a   # fixed by the margins
            continue
        lo, pmf = _hypergeom_pmf(events, n1, n2)
        offset += lo
        dist = np.convolve(dist, pmf)
    k = observed - offset
    if k <= 0:
        return 1.0
    if k >= len(dist):
        return 0.0
    return float(min(1.0, dist[k:].sum()))


def fisher_greater(a: int, n1: int, c: int, n2: int) -> float:
    """One-sided Fisher exact p: candidate rate greater than stable."""
    return stratified_exact_greater([(a, n1, c, n2)])


def fisher_two_sided(a: int, n1: int, c: int, n2: int) -> float:
    """Two-sided Fisher exact p (sum of tables no more likely than the observed one)."""
    events = a + c
    if events == 0 or n1 == 0 or n2 == 0 or events == n1 + n2:
        return 1.0
    lo, pmf = _hypergeom_pmf(events, n1, n2)
    idx = a - lo
    if idx < 0 or idx >= len(pmf):
        p_obs = 0.0
    else:
        p_obs = pmf[idx]
    return float(min(1.0, pmf[pmf <= p_obs * (1.0 + 1e-7)].sum()))


def rd_mh(strata: Iterable[Stratum]) -> float:
    """Mantel-Haenszel risk difference: sum w_k (a/n1 - c/n2) / sum w_k, w_k = n1 n2 / (n1 + n2)."""
    num = den = 0.0
    for a, n1, c, n2 in strata:
        if n1 == 0 or n2 == 0:
            continue
        w = n1 * n2 / (n1 + n2)
        num += w * (a / n1 - c / n2)
        den += w
    return num / den if den else 0.0


def binom_greater(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return betainc(k, n - k + 1, p)


def _log_comb(a: int, b: int) -> float:
    return math.lgamma(a + 1) - math.lgamma(b + 1) - math.lgamma(a - b + 1)


def exceedance_greater(k: int, n_c: int, n_s: int, m: int) -> float:
    """P(K >= k) for K = the number of n_c candidate values above the m-th smallest of n_s stable values, when
    both arms come from the same continuous distribution (the exceedance statistic, Gumbel and von Schelling 1950):
    P(K = j) = C(m - 1 + n_c - j, n_c - j) C(n_s - m + j, j) / C(n_s + n_c, n_c). Distribution-free, so it holds
    for right-skewed session latencies at any sample size, where comparing an exceedance count with a fixed
    5% would not: a small sample's p95 is close to its maximum, which a new value exceeds with probability
    (n_s - m + 1) / (n_s + 1), 1/9 at n_s = 8."""
    if not 1 <= m <= n_s or n_c < 0:
        raise ValueError("need 1 <= m <= n_s and n_c >= 0")
    if k <= 0:
        return 1.0
    if k > n_c:
        return 0.0
    total = _log_comb(n_s + n_c, n_c)
    terms = [_log_comb(m - 1 + n_c - j, n_c - j) + _log_comb(n_s - m + j, j) - total for j in range(k, n_c + 1)]
    top = max(terms)
    return min(1.0, math.exp(top) * sum(math.exp(t - top) for t in terms))


def clopper_pearson_upper(k: int, n: int, confidence: float = 0.95) -> float:
    """Upper end of the two-sided Clopper-Pearson interval."""
    if n <= 0:
        raise ValueError("n must be positive")
    if k >= n:
        return 1.0
    return beta_ppf(1.0 - (1.0 - confidence) / 2.0, k + 1, n - k)


# ---------------------------------------------------------------- t tests
def welch_less(m1: float, v1: float, n1: int, m2: float, v2: float, n2: int) -> float:
    """One-sided Welch p for 'mean 1 < mean 2' from means, sample variances and sizes."""
    if n1 < 2 or n2 < 2:
        return 1.0
    se2 = v1 / n1 + v2 / n2
    if se2 <= 0:
        return 0.0 if m1 < m2 else 1.0
    t = (m1 - m2) / math.sqrt(se2)
    df = se2 ** 2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1)) if (v1 > 0 or v2 > 0) else n1 + n2 - 2
    return t_cdf(t, df)


def welch_greater(m1: float, v1: float, n1: int, m2: float, v2: float, n2: int) -> float:
    """One-sided Welch p for 'mean 1 > mean 2'."""
    return welch_less(m2, v2, n2, m1, v1, n1)


def paired_less(deltas: Sequence[float]) -> float:
    """One-sided paired t p for 'mean delta < 0'."""
    n = len(deltas)
    if n < 2:
        return 1.0
    m, s = mean(deltas), sd(deltas)
    if s == 0:
        return 0.0 if m < 0 else 1.0
    return t_cdf(m / (s / math.sqrt(n)), n - 1)
