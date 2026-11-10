"""Deterministic statistics. No randomness anywhere: the same counts always give the same decision.

Comparisons use the Mantel-Haenszel risk ratio stratified by (day, lead feature code, the other
agent dimension, prompt). Stratification matters: once cohorts get different allocations, a raw
"voice V2 vs V1" comparison would mix populations (Simpson's paradox). Within a stratum the
simulator randomises variants independently, so each stratum is a clean mini-experiment.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class Cmp:
    rr: float          # MH risk ratio arm / control
    log_rr: float
    se: float          # Greenland-Robins SE of log RR
    z: float
    n_arm: int         # trials in arm (connected calls for engagement)
    n_ctl: int
    rate_arm: float
    rate_ctl: float

    @property
    def p_better(self) -> float:      # one-sided p-value for H0: rr <= 1
        return norm_sf(self.z)

    @property
    def p_worse(self) -> float:       # one-sided p-value for H0: rr >= 1
        return norm_sf(-self.z)

    def ci(self, z=1.645):
        return math.exp(self.log_rr - z * self.se), math.exp(self.log_rr + z * self.se)

    def as_dict(self):
        lo, hi = self.ci()
        return {"lift": round(self.rr - 1, 4), "ci90": [round(lo - 1, 4), round(hi - 1, 4)], "z": round(self.z, 2),
                "n_arm": self.n_arm, "n_ctl": self.n_ctl, "rate_arm": round(self.rate_arm, 4),
                "rate_ctl": round(self.rate_ctl, 4)}


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def mh(a_succ, a_n, c_succ, c_n) -> Cmp | None:
    """Mantel-Haenszel RR over strata (arrays of equal shape, any dims). None if not estimable."""
    a_succ = np.asarray(a_succ, float).ravel()
    a_n = np.asarray(a_n, float).ravel()
    c_succ = np.asarray(c_succ, float).ravel()
    c_n = np.asarray(c_n, float).ravel()
    keep = (a_n > 0) & (c_n > 0)
    if not keep.any():
        return None
    a, n1, c, n0 = a_succ[keep], a_n[keep], c_succ[keep], c_n[keep]
    N = n1 + n0
    R = (a * n0 / N).sum()
    S = (c * n1 / N).sum()
    if R <= 0 or S <= 0:
        return None
    rr = R / S
    var = ((n1 * n0 * (a + c) - a * c * N) / N ** 2).sum() / (R * S)
    se = math.sqrt(max(var, 1e-12))
    lr = math.log(rr)
    return Cmp(rr=rr, log_rr=lr, se=se, z=lr / se, n_arm=int(n1.sum()), n_ctl=int(n0.sum()),
               rate_arm=float(a.sum() / n1.sum()), rate_ctl=float(c.sum() / n0.sum()))


def holm(pvals: list[float], alpha: float) -> list[bool]:
    """Holm-Bonferroni step-down: controls family-wise error at `alpha`."""
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    out = [False] * len(pvals)
    m = len(pvals)
    for rank, i in enumerate(order):
        if pvals[i] <= alpha / (m - rank):
            out[i] = True
        else:
            break
    return out


def shrink(log_rr: float, se: float, prior_sd: float = 0.05) -> float:
    """Normal-normal shrinkage of a log lift toward 0. Used for impact estimates, never for
    deciding winners: it keeps the numbers we show approvers honest (no winner's curse)."""
    w = prior_sd ** 2 / (prior_sd ** 2 + se ** 2)
    return log_rr * w


def poisson_sf(k: int, mu: float) -> float:
    """P(X >= k) for X ~ Poisson(mu)."""
    if k <= 0:
        return 1.0
    term = math.exp(-mu)
    cdf = term
    for i in range(1, k):
        term *= mu / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def two_prop_z(x1, n1, x0, n0) -> float:
    if n1 <= 0 or n0 <= 0:
        return 0.0
    p = (x1 + x0) / (n1 + n0)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n0), 1e-12))
    return (x1 / n1 - x0 / n0) / se


def n_per_arm(p: float, rel_lift: float, alpha: float, power: float = 0.8) -> int:
    """Connected calls per arm to detect a relative lift with a one-sided test."""
    za = -_ppf(alpha)
    zb = _ppf(power)
    d = p * rel_lift
    return int(math.ceil((za + zb) ** 2 * 2 * p * (1 - p) / d ** 2))


def _ppf(q: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 1 - norm_sf(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
