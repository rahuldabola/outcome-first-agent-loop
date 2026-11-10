"""Call-time optimisation (C4), per cohort, bootstrapped from pre-D0 history (C3).

We do not change *how many* calls happen or *when the window is* (business-owned). We only
change *who* the dialler prefers in a given hour. For the feature f whose values differ most in
their hour-of-day pickup pattern (picked from data, not assumed), each value v gets an hour table

    w[v][h] = (r[v,h] / geo-mean_h r[v,h]) ** GAMMA,   r[v,h] = pickup(v,h) / pickup(all,h)

r is *relative to everyone dialled in the same hour*, which cancels hour-level effects such as
"later hours are full of retries of people who did not pick up earlier". Each table is normalised
to mean 1 over the window, so no group is starved or prioritised overall: we only shift a group
toward its own better hours. All in-window hours keep weight >= 0.25 (an omitted hour would get
weight 0 and silently waste capacity).

Per-person preferred hours were cut: a person gets <= ~8 attempts per campaign, which cannot
identify a 12-hour profile (see SUBMISSION.md).
"""
from __future__ import annotations

import math

import numpy as np

from .features import FEATURES, mask

GAMMA = 1.5
SHRINK_M = 200          # pseudo-dials pulling a (value, hour) cell toward the hour mean
MIN_SPREAD = 0.06       # weighted sd of log r below this -> timing is not worth a change
CLIP = (0.25, 4.0)


def fit(w, lo_day: int, hi_day: int) -> dict | None:
    P = w.days("pick", lo_day, hi_day)          # [hour, code, did, (dials, conn, eng)]
    if P is None:
        return None
    lo, hi = w.spec["dialling_window"]
    hours = list(range(lo, hi))
    dials = P[..., 0].sum(axis=2)               # [hour, code]
    conn = P[..., 1].sum(axis=2)
    p_h = conn[hours].sum(axis=1) / np.maximum(dials[hours].sum(axis=1), 1)   # [H]
    best = None
    for f, vals in FEATURES.items():
        tabs, spread_num, spread_den = {}, 0.0, 0.0
        for v in vals:
            m = mask({f: [v]})
            d = dials[hours][:, m].sum(axis=1)
            if d.sum() < 50 * len(hours):
                continue
            c = conn[hours][:, m].sum(axis=1)
            p = (c + SHRINK_M * p_h) / (d + SHRINK_M)
            lr = np.log(np.maximum(p, 1e-6) / np.maximum(p_h, 1e-6))
            lr = lr - lr.mean()
            spread_num += float((d * lr ** 2).sum())
            spread_den += float(d.sum())
            tabs[v] = {str(h): float(np.clip(math.exp(GAMMA * x), *CLIP)) for h, x in zip(hours, lr)}
        if len(tabs) < 2 or spread_den == 0:
            continue
        spread = math.sqrt(spread_num / spread_den)
        if best is None or spread > best["spread"]:
            best = {"feature": f, "weights": tabs, "spread": round(spread, 4)}
    if best is None or best["spread"] < MIN_SPREAD:
        return None
    # renormalise each table to mean 1 and round for a readable diff
    for v, t in best["weights"].items():
        mu = sum(t.values()) / len(t)
        best["weights"][v] = {h: round(x / mu, 2) for h, x in t.items()}
    best["estimate"] = estimate_lift(dials[hours], conn[hours], best)
    best["fit_days"] = [lo_day, hi_day]
    return best


def estimate_lift(dials, conn, sched) -> float:
    """Back-of-envelope pickup lift if each hour's capacity were re-shared by the weights,
    assuming the pool of eligible leads per hour looked like history. Optimistic by design
    (it ignores that moving a lead to hour A removes it from hour B's pool) -- reported as an
    upper bound, and verified after apply from real pickup, not from this number."""
    f = sched["feature"]
    base = new = 0.0
    hours = list(sched["weights"][next(iter(sched["weights"]))].keys())
    for hi_, h in enumerate(hours):
        tot_d = dials[hi_].sum()
        if tot_d == 0:
            continue
        num_w, den_w, base_c = 0.0, 0.0, conn[hi_].sum()
        for v, t in sched["weights"].items():
            m = mask({f: [v]})
            d, c = dials[hi_][m].sum(), conn[hi_][m].sum()
            if d == 0:
                continue
            num_w += d * t[h] * (c / d)
            den_w += d * t[h]
        base += base_c
        new += tot_d * num_w / max(den_w, 1e-9)
    return round(new / base - 1, 4) if base else 0.0
