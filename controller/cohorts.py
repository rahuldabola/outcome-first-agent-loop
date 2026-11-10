"""Builds the cohort list + allocations + schedule of a proposed config from learned state.

The shared cohort object
------------------------
A *segment* is a feature predicate (e.g. {"lang": ["ta"]}) with a canonical id from
`features.seg_key`. Experiments test segments, rules are attached to segments, and the supervisor
view reports by segment id. The dialler, however, needs a list of mutually exclusive cohorts
(first match wins), and a lead can only be in one. So the config's cohorts are the *cells* of the
coarsest partition that respects every segment in use: each segment is then an exact union of
cells, and every cell inherits the decisions of every segment containing it. Cell ids are
`au:<seg_key>` so anyone can read which population a cohort is.

Cohorts we did not create (no `au:` prefix, e.g. a supervisor's) are kept, unchanged, in front
of ours so they keep precedence.
"""
from __future__ import annotations

import itertools

from .features import FEATURES, FEAT_NAMES, quantize, seg_key

MAX_COHORTS = 20
PREFIX = "au:"


def contains(seg: dict, cell: dict) -> bool:
    """Does segment `seg` contain every lead of `cell`? (cell is a partition cell)"""
    for f, vals in seg.items():
        if f not in cell or not set(cell[f]) <= set(vals):
            return False
    return True


def refine(groups: list[list[str]], s: list[str]) -> list[list[str]]:
    out = []
    for g in groups:
        a = [v for v in g if v in s]
        b = [v for v in g if v not in s]
        out += [x for x in (a, b) if x]
    return out


def partitions(segs: list[dict], singleton_feature: str | None) -> dict[str, list[list[str]]]:
    parts: dict[str, list[list[str]]] = {}
    for s in segs:
        for f, vals in s.items():
            parts.setdefault(f, [list(FEATURES[f])])
            parts[f] = refine(parts[f], vals)
    if singleton_feature:
        parts[singleton_feature] = [[v] for v in FEATURES[singleton_feature]]
    return {f: g for f, g in parts.items() if len(g) > 1}


def cells_of(parts: dict[str, list[list[str]]]) -> list[dict]:
    feats = [f for f in FEAT_NAMES if f in parts]
    return [dict(zip(feats, combo)) for combo in itertools.product(*[parts[f] for f in feats])] if feats else []


def alloc_for(cell: dict | None, dim: str, st: dict, explore_each: float, explore_cap: float,
              holdout: float) -> tuple[dict, str]:
    """Distribution over a dim's options for one cell (None = population default).
    Returns (dist, base_arm)."""
    ctl = st["control"]
    harmed = {h["arm"] for h in st.get("harm", []) if cell is not None and contains(h["seg"], cell)
              or not h["seg"]}
    decayed = {d["arm"] for d in st.get("decayed", []) if not d["seg"] or (cell is not None and contains(d["seg"], cell))}
    base, best = ctl, 0.0
    for r in st.get("rules", []):
        applies = (not r["seg"]) or (cell is not None and contains(r["seg"], cell))
        if applies and r["arm"] not in harmed and r["arm"] not in decayed and r["lift"] > best:
            base, best = r["arm"], r["lift"]
    explore = [a for a in st.get("active", []) if a not in harmed and a not in decayed and a not in (base, ctl)]
    dist: dict[str, float] = {}
    want = {a: explore_each for a in explore}
    scale = min(1.0, explore_cap / sum(want.values())) if want else 1.0
    for a in explore:
        dist[a] = round(want[a] * scale, 4)
    if base != ctl:
        dist[ctl] = holdout
    dist[base] = round(1.0 - sum(dist.values()), 4)
    return quantize(dist), base


def build(current: dict, exp: dict, schedule: dict | None, dims: list[str], caller_dist: dict | None,
          explore_each=0.10, explore_cap=0.40, holdout=0.20) -> tuple[dict, list[str]]:
    """Returns (proposed_config, notes). `exp[dim]` is the experiment state for that dim."""
    notes: list[str] = []
    foreign = [c for c in current.get("cohorts") or [] if not c["id"].startswith(PREFIX)]
    room = MAX_COHORTS - len(foreign)

    segs_rules = [r["seg"] for d in dims for r in exp[d].get("rules", []) if r["seg"]]
    # a segment carve-out only matters for an arm that is still live somewhere
    live = {d: set(exp[d].get("active", [])) | {r["arm"] for r in exp[d].get("rules", [])} for d in dims}
    segs_harm = [h["seg"] for d in dims for h in exp[d].get("harm", []) if h["seg"] and h["arm"] in live[d]]
    segs_decay = [x["seg"] for d in dims for x in exp[d].get("decayed", []) if x["seg"]]
    sched_feat = schedule["feature"] if schedule else None

    def n_cells(segs, sf):
        p = partitions(segs, sf)
        n = 1
        for g in p.values():
            n *= len(g)
        return n if p else 0

    segs = segs_rules + segs_harm + segs_decay
    if n_cells(segs, sched_feat) > room and sched_feat:
        notes.append(f"schedule partition on {sched_feat} dropped: would exceed {MAX_COHORTS} cohorts")
        sched_feat = None
    if n_cells(segs, sched_feat) > room:
        # last resort: drop segment-level harm carve-outs (arm then stops being explored anywhere)
        for d in dims:
            for h in exp[d].get("harm", []):
                if h["seg"] and h["arm"] in exp[d].get("active", []):
                    exp[d]["active"].remove(h["arm"])
                    notes.append(f"{d}={h['arm']} removed from exploration everywhere (cohort budget)")
        segs = segs_rules + segs_decay
    while n_cells(segs, sched_feat) > room and segs_rules:
        segs_rules = segs_rules[:-1]
        segs = segs_rules + segs_decay
        notes.append("lowest-priority segment rule not expressed (cohort budget)")

    parts = partitions(segs, sched_feat)
    cells = cells_of(parts)

    cfg = {"cohorts": [dict(c) for c in foreign], "allocations": {}, "prompt_version": current["prompt_version"],
           "schedule": {"hour_weights": {}, "lead_preferred_hour": dict(current.get("schedule", {}).get(
               "lead_preferred_hour") or {})}}
    cur_alloc = current.get("allocations") or {}
    for c in foreign:
        if c["id"] in cur_alloc:
            cfg["allocations"][c["id"]] = cur_alloc[c["id"]]
        hw = (current.get("schedule") or {}).get("hour_weights", {}).get(c["id"])
        if hw is not None:
            cfg["schedule"]["hour_weights"][c["id"]] = hw

    default = {}
    for d in dims:
        default[d], _ = alloc_for(None, d, exp[d], explore_each, explore_cap, holdout)
    for d in ("welcome_message", "voice"):
        if d not in default:
            default[d] = dict(cur_alloc.get("default", {}).get(d) or {})
    default["caller_id"] = caller_dist or dict(cur_alloc.get("default", {}).get("caller_id") or {})
    cfg["allocations"]["default"] = default

    for cell in cells:
        cid = PREFIX + seg_key(cell)
        cfg["cohorts"].append({"id": cid, "where": cell})
        a = {}
        for d in dims:
            dist, _ = alloc_for(cell, d, exp[d], explore_each, explore_cap, holdout)
            if dist != default[d]:
                a[d] = dist
        if a:
            cfg["allocations"][cid] = a
        if schedule and sched_feat:
            vals = cell.get(sched_feat, FEATURES[sched_feat])
            tables = [schedule["weights"][v] for v in vals if v in schedule["weights"]]
            if tables:
                hours = sorted({h for t in tables for h in t}, key=int)
                cfg["schedule"]["hour_weights"][cid] = {
                    h: round(sum(t.get(h, 1.0) for t in tables) / len(tables), 3) for h in hours}
    return cfg, notes
