"""Turns a proposed config into an approval request a busy supervisor can say yes to (C10):
summary, structured diff, affected-lead count, dry run against recent real traffic, impact score.

Honesty rules for the numbers we show:
  * Expected lifts use *shrunk* estimates of accepted winners only. Arms we are merely exploring
    are assumed equal to control, so an exploration-only request shows ~0 impact rather than an
    invented upside. Ranking a queue by impact therefore ranks proven changes first.
  * impact_score = clip(expected relative lift of the workflow's PRIMARY KPI / 5%, 0, 1).
    0.2 ~ +1% on the primary KPI across the workflow; 1.0 = +5% or more. Incidents score 1.0.
  * affected_leads counts real, still-dialable leads whose dialling changes, plus the next daily
    batch for workflows that lock the agent config per campaign (mid_flight next_campaign_only).
"""
from __future__ import annotations

import json

import numpy as np

from . import experiments as X
from .features import cohort_index, dist_for, effective_dists

AGENT_DIMS = ("welcome_message", "voice", "caller_id")


def canon(cfg: dict) -> str:
    """Canonical form for equality (weights normalised + rounded, hours as ints)."""
    c = json.loads(json.dumps(cfg))
    for k, dims in (c.get("allocations") or {}).items():
        for d, ws in list(dims.items()):
            s = sum(ws.values()) or 1
            dims[d] = {o: round(v / s, 3) for o, v in sorted(ws.items()) if v / s > 1e-6}
    hw = (c.get("schedule") or {}).get("hour_weights") or {}
    for k, t in hw.items():
        hw[k] = {str(int(h)): round(float(v), 3) for h, v in t.items()}
    c.setdefault("schedule", {}).setdefault("lead_preferred_hour", {})
    c.setdefault("cohorts", [])
    return json.dumps(c, sort_keys=True)


def changed_dims(old: dict, new: dict) -> list[str]:
    out = []
    if json.dumps(old.get("cohorts") or [], sort_keys=True) != json.dumps(new.get("cohorts") or [], sort_keys=True):
        out.append("cohorts")
    for d in AGENT_DIMS:
        if [json.dumps(x, sort_keys=True) for x in _rounded(effective_dists(old, d))] != \
                [json.dumps(x, sort_keys=True) for x in _rounded(effective_dists(new, d))]:
            out.append(d)
    if old.get("prompt_version") != new.get("prompt_version"):
        out.append("prompt_version")
    if canon({"schedule": old.get("schedule") or {}}) != canon({"schedule": new.get("schedule") or {}}):
        out.append("schedule")
    return out


def _rounded(ds):
    return [{k: round(v, 3) for k, v in sorted(d.items())} for d in ds]


def diff(old: dict, new: dict) -> dict:
    out: dict = {}
    oc = {c["id"]: c["where"] for c in old.get("cohorts") or []}
    nc = {c["id"]: c["where"] for c in new.get("cohorts") or []}
    if oc != nc:
        out["cohorts"] = {"added": sorted(set(nc) - set(oc)), "removed": sorted(set(oc) - set(nc))}
    al = {}
    for k in sorted(set(old.get("allocations", {})) | set(new.get("allocations", {}))):
        for d in AGENT_DIMS:
            a = dist_for(old, k, d) if k == "default" or k in oc else {}
            b = dist_for(new, k, d) if k == "default" or k in nc else {}
            a = {o: round(v, 3) for o, v in a.items()}
            b = {o: round(v, 3) for o, v in b.items()}
            if a != b:
                al.setdefault(k, {})[d] = {"from": a or None, "to": b or None}
    if al:
        out["allocations"] = al
    if old.get("prompt_version") != new.get("prompt_version"):
        out["prompt_version"] = {"from": old.get("prompt_version"), "to": new.get("prompt_version")}
    os_, ns_ = (old.get("schedule") or {}).get("hour_weights") or {}, (new.get("schedule") or {}).get("hour_weights") or {}
    if canon({"schedule": {"hour_weights": os_}}) != canon({"schedule": {"hour_weights": ns_}}):
        out["schedule"] = {"hour_weights_for": sorted(ns_), "previously_for": sorted(os_)}
    return out


def affected_leads(w, old: dict, new: dict, today: int, dims: list[str]) -> int:
    active = w.active_by_code(today)
    changed = np.zeros(len(active), dtype=bool)
    agent = [d for d in AGENT_DIMS if d in dims] or (list(AGENT_DIMS) if "cohorts" in dims else [])
    for d in agent:
        a, b = effective_dists(old, d), effective_dists(new, d)
        changed |= np.array([_r(x) != _r(y) for x, y in zip(a, b)])
    locked = w.spec.get("mid_flight") == "next_campaign_only"
    n = 0
    if agent and not locked:
        n += int(active[changed].sum())
    elif agent and changed.any():
        # agent config is locked per campaign: only tomorrow's batch (and later) is affected
        share = active[changed].sum() / max(active.sum(), 1) if active.sum() else changed.mean()
        n += int(round(w.spec["daily_batch"]["size"] * share))
    if "schedule" in dims:
        n_old, idx_old = cohort_index(old)
        n_new, idx_new = cohort_index(new)
        ho = (old.get("schedule") or {}).get("hour_weights") or {}
        hn = (new.get("schedule") or {}).get("hour_weights") or {}
        tab = lambda hw, names, i: json.dumps(hw.get(names[i], hw.get("default")), sort_keys=True)
        sch = np.array([tab(ho, n_old, idx_old[c]) != tab(hn, n_new, idx_new[c]) for c in range(len(active))])
        n = max(n, int(active[sch | changed].sum()))
    if "prompt_version" in dims:
        n = int(active.sum())
    return n


def _r(d):
    return {k: round(v, 3) for k, v in d.items()}


def dry_run(w, exp: dict, old: dict, new: dict, today: int, did_health: dict | None, sched_est: float | None):
    """Replay the last 2 days of real connected calls (by lead feature code) under both configs."""
    E = w.days("eng", today - 2, today)
    out = {"method": "replay of real connected calls from the last 2 days, re-weighted by each config's "
                     "per-cohort allocation; arms not yet proven assumed equal to control"}
    if E is None:
        out["note"] = "no recent traffic"
        return out, 0.0, 0.0
    by_code = E[..., 0].reshape(E.shape[0], -1).sum(axis=1).astype(float)
    eng = E[..., 1].sum()
    conn = E[..., 0].sum()
    base_rate = eng / max(conn, 1)
    m_old = X.expected_mult(old, exp, w, by_code)
    m_new = X.expected_mult(new, exp, w, by_code)
    e_lift = m_new / m_old - 1
    out.update({"replayed_connected_calls": int(conn), "observed_engagement": round(float(base_rate), 4),
                "expected_engagement_current": round(float(base_rate), 4),
                "expected_engagement_proposed": round(float(base_rate * m_new / m_old), 4),
                "expected_engagement_lift": round(float(e_lift), 4)})
    p_lift = 0.0
    if did_health:
        def hmult(cfg):
            dist = dist_for(cfg, "default", "caller_id")
            return sum(p * did_health.get(d, 1.0) for d, p in dist.items())
        ho, hn = hmult(old), hmult(new)
        if ho > 0 and abs(hn / ho - 1) > 1e-4:
            p_lift += hn / ho - 1
            out["caller_id_pickup_lift"] = round(hn / ho - 1, 4)
    if sched_est:
        p_lift += sched_est
        out["schedule_pickup_lift_upper_bound"] = sched_est
    out["expected_pickup_lift"] = round(p_lift, 4)
    # per-cohort view for the supervisor: who changes and by how much
    names, idx = cohort_index(new)
    rows = []
    for i, nme in enumerate(names):
        sel = idx == i
        wsum = by_code[sel].sum()
        if wsum == 0:
            continue
        cw = np.where(sel, by_code, 0)
        mo, mn = X.expected_mult(old, exp, w, cw), X.expected_mult(new, exp, w, cw)
        rows.append({"cohort": nme, "share_of_calls": round(float(wsum / by_code.sum()), 3),
                     "expected_engagement_lift": round(float(mn / mo - 1), 4)})
    out["per_cohort"] = rows
    return out, float(e_lift), float(p_lift)


def impact(primary: str, e_lift: float, p_lift: float) -> float:
    lift = e_lift if primary == "engagement_rate" else p_lift
    return round(float(min(1.0, max(0.0, lift / 0.05))), 3)
