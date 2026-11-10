"""Caller-ID (DID) health (C5): detect degradation, rotate away, explore unknown numbers safely.

Caller IDs are assigned to calls at random by the dialler, independently of who is called, so
comparing DIDs is a randomised comparison. We still stratify by (hour, lead code) with
Mantel-Haenszel because the allocation itself may have changed inside the window.

Status per DID (with hysteresis -- a number never flips back and forth on noise):
  unknown   never (or barely) used. Probed one at a time at 5% of traffic.
  healthy   >= 300 dials in the last 2 days and not flagged.
  flagged   pickup < 0.70x the other numbers with z <= -4 over the last 6 dialling hours
            (looks spam-flagged) -> removed at once, alert raised.
  poor      a probe that came back < 0.85x the others (z <= -2) -> not adopted.
  flagged/poor are sticky: never re-probed automatically (a human clears them after carrier
            remediation). Healthy numbers that fall idle for 48 h become 'unknown' again.

Routing among healthy numbers: weight ~ (shrunk relative pickup)^3. Numbers that answer more get
more traffic; as their volume rises their pickup falls (carriers penalise high-volume numbers), so
the split self-balances. A change is only made if our estimate of its immediate effect on pickup
is non-negative (rotating a flagged number out is always allowed), at most every 4 h, and only if
it moves >= 5% of traffic -- this is what stops flapping.
"""
from __future__ import annotations

import numpy as np

from . import stats
from .features import quantize

PROBE = 0.05
MIN_DIALS_KNOWN = 300
REST_MINUTES = 48 * 60
MIN_TV_CHANGE = 0.05
SHRINK_DIALS = 1500
POWER = 3.0


def recent(w, minute: int, n_hours: int) -> np.ndarray | None:
    """pick cube rows [hour, code, did, 3] for the last `n_hours` clock hours that had dials."""
    out = []
    for k in range(1, 72 + 1):
        t = minute // 60 - k
        d, h = divmod(t, 24)
        a = w.pick.get(d)
        if a is None or a[h, :, :, 0].sum() == 0:
            continue
        out.append(a[h])
        if len(out) >= n_hours:
            break
    return np.stack(out).astype(np.int64) if out else None


def _recent_dials(w, minute: int, i: int) -> int:
    day = minute // 1440
    return sum(int(w.pick[d][:, :, i, 0].sum()) for d in (day - 1, day) if d in w.pick)


def _vs_others(R, i, n_dids):
    if R is None:
        return None, 0
    dials = int(R[:, :, i, 0].sum())
    others = [j for j in range(n_dids) if j != i and R[:, :, j, 0].sum() > 0]
    if dials < 100 or not others:
        return None, dials
    a = R[:, :, i, :]
    c = R[:, :, others, :].sum(axis=2)
    return stats.mh(a[..., 1], a[..., 0], c[..., 1], c[..., 0]), dials


def assess(w, minute: int, state: dict, cur_dist: dict) -> dict:
    """Returns {did: {status, ratio, z, dials, health}} and updates `state` (persisted by caller).

    `health` = pickup relative to the workflow's other numbers over the last 12 dialling hours,
    shrunk toward 1 by 1500 pseudo-dials so routing does not chase noise."""
    R6, R12 = recent(w, minute, 6), recent(w, minute, 12)
    status = state.setdefault("status", {})
    out = {}
    dids = w.opts["caller_id"]
    for i, d in enumerate(dids):
        st = status.setdefault(d, {"status": "unknown", "since": minute})
        info = {"dials": 0, "health": 1.0}
        c6, _ = _vs_others(R6, i, len(dids))
        c12, n12 = _vs_others(R12, i, len(dids))
        info["dials"] = n12
        if c12 is not None:
            info.update(ratio=round(c12.rr, 3), z=round(c12.z, 2))
            info["health"] = round(1 + (c12.rr - 1) * n12 / (n12 + SHRINK_DIALS), 4)
        if c6 is not None and c6.rr < 0.70 and c6.z <= -4 and st["status"] not in ("flagged", "poor"):
            st.update(status="flagged", since=minute, ratio=round(c6.rr, 3), z=round(c6.z, 2))
        if st["status"] in ("unknown", "resting") and _recent_dials(w, minute, i) >= MIN_DIALS_KNOWN \
                and c12 is not None:
            if c12.rr < 0.85 and c12.z <= -2:
                st.update(status="poor", since=minute, ratio=info["ratio"], z=info["z"])
            else:
                st.update(status="healthy", since=minute)
        if st["status"] == "healthy" and cur_dist.get(d, 0) == 0 and _recent_dials(w, minute, i) == 0 \
                and minute - st["since"] > REST_MINUTES:
            st.update(status="unknown", since=minute)   # unused for long: we no longer know it
        # 'poor' and 'flagged' are sticky. A probe runs at 5% of traffic, i.e. at negligible load, so a
        # number that answers poorly *while probed* is poor in itself, not overloaded; re-probing it
        # would only spend customers. A human clears either state (see RUNBOOK.md).
        # 'flagged' is sticky: carrier spam labels rarely clear on their own, and every probe call
        # on a flagged number is a customer who sees "Spam likely". A human clears it after the
        # carrier remediation (see RUNBOOK.md).
        info["status"] = st["status"]
        out[d] = info
    return out


def target(health: dict, cur: dict, max_probes: int = 1) -> dict:
    """Desired allocation. Never routes to a flagged/poor number; probes <= max_probes at 5% each."""
    bad = ("flagged", "poor")
    healthy = [d for d, h in health.items() if h["status"] == "healthy"]
    if not healthy:
        healthy = [d for d in cur if health.get(d, {}).get("status") not in bad + ("unknown", "resting")] or \
                  [d for d in cur if health.get(d, {}).get("status") not in bad]
    if not healthy:
        return dict(cur)
    probing = [d for d in cur if health.get(d, {}).get("status") in ("unknown", "resting") and cur.get(d, 0) > 0
               and d not in healthy]
    cands = [d for d, h in health.items() if h["status"] in ("unknown", "resting") and d not in healthy
             and d not in probing]
    probe = (probing + cands)[:max_probes]
    w = {d: max(health[d].get("health", 1.0), 0.05) ** POWER for d in healthy}
    s = sum(w.values())
    share = 1 - PROBE * len(probe)
    out = {d: share * v / s for d, v in w.items()}
    for p in probe:
        out[p] = PROBE
    return quantize(out)


def worth_it(cur: dict, new: dict, health: dict) -> bool:
    """Change caller IDs only for a reason: a flagged/poor number is live, the set of numbers in use
    changes (probe starts/ends), or the estimated pickup gain is >= 1%."""
    if any(health.get(d, {}).get("status") in ("flagged", "poor") for d, v in cur.items() if v > 0):
        return True
    used_cur = {d for d, v in cur.items() if v > 0}
    used_new = {d for d, v in new.items() if v > 0}
    if used_cur != used_new:
        return expected_effect(cur, new, health) >= -0.005
    if tv(cur, new) < MIN_TV_CHANGE:
        return False
    return expected_effect(cur, new, health) >= 0.01


def expected_effect(cur: dict, new: dict, health: dict) -> float:
    """Our estimate of the immediate relative pickup change (unknown numbers assumed average)."""
    def m(dist):
        s = sum(dist.values()) or 1
        return sum(v / s * health.get(d, {}).get("health", 1.0) for d, v in dist.items())
    return m(new) / max(m(cur), 1e-9) - 1


def tv(a: dict, b: dict) -> float:
    sa, sb = sum(a.values()) or 1, sum(b.values()) or 1
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0) / sa - b.get(k, 0) / sb) for k in keys)


def step_toward(cur: dict, tgt: dict, max_shift: float) -> dict:
    """Largest move from cur toward tgt with total-variation distance <= max_shift."""
    sc = sum(cur.values()) or 1
    c = {k: v / sc for k, v in cur.items()}
    d = tv(c, tgt)
    if d <= max_shift + 1e-9:
        return dict(tgt)
    f = (max_shift - 1e-3) / d
    keys = set(c) | set(tgt)
    out = {k: c.get(k, 0) + f * (tgt.get(k, 0) - c.get(k, 0)) for k in keys}
    out = quantize({k: v for k, v in out.items() if v > 1e-6})
    if tv(c, out) > max_shift:   # quantisation can add up to ~1/1024 per option
        return step_toward(cur, tgt, max_shift - 0.01)
    return out
