"""Lead feature encoding and cohort predicates.

Every lead is reduced to one integer "feature code" (0..251) = the cell of the full
lang x city_tier x age_band x value_band cross-product. All statistics are kept per code,
so ANY cohort predicate (an AND across features, OR within a feature) can be evaluated
after the fact as a boolean mask over 252 codes. This is what makes the cohort a single
shared object: detection, experiments, dry runs and the supervisor view all evaluate the
same predicate against the same per-code counts.
"""
from __future__ import annotations

import itertools

import numpy as np

# The feature domain is part of the public contract (lead events / cohort `where` clauses).
FEATURES: dict[str, list[str]] = {
    "lang": ["hi", "en", "ta", "te", "kn", "mr", "bn"],
    "city_tier": ["1", "2", "3"],
    "age_band": ["18-30", "31-45", "46-60", "60+"],
    "value_band": ["low", "mid", "high"],
}
FEAT_NAMES = list(FEATURES)
SIZES = [len(FEATURES[f]) for f in FEAT_NAMES]
N_CODES = int(np.prod(SIZES))  # 252

# CODE_VALUES[f][code] -> value index of feature f for that code
_grid = np.array(list(itertools.product(*[range(s) for s in SIZES])), dtype=np.int16)
CODE_VALUES = {f: _grid[:, i] for i, f in enumerate(FEAT_NAMES)}
_VIDX = {f: {v: i for i, v in enumerate(vals)} for f, vals in FEATURES.items()}
# Ordinal features: contiguous ranges are meaningful candidate segments.
ORDINAL = {"age_band", "city_tier", "value_band"}


def encode(feats: dict) -> int:
    """features dict -> code, or -1 if any value is outside the known domain."""
    c = 0
    try:
        for f, s in zip(FEAT_NAMES, SIZES):
            c = c * s + _VIDX[f][feats[f]]
    except KeyError:
        return -1
    return c


def mask(where: dict | None) -> np.ndarray:
    """Cohort predicate -> bool[252]. Empty/None predicate matches everything."""
    m = np.ones(N_CODES, dtype=bool)
    for f, vals in (where or {}).items():
        idx = [_VIDX[f][v] for v in vals if v in _VIDX.get(f, {})]
        m &= np.isin(CODE_VALUES[f], idx)
    return m


def seg_key(where: dict | None) -> str:
    """Canonical, human-readable id for a predicate, e.g. 'age_band=46-60|60+;lang=ta'.

    Values are written in domain order so the same population always gets the same id."""
    if not where:
        return "ALL"
    parts = []
    for f in FEAT_NAMES:
        if f in where:
            vals = [v for v in FEATURES[f] if v in where[f]]
            parts.append(f"{f}={'|'.join(vals)}")
    return ";".join(parts)


def parse_seg(key: str) -> dict:
    if key == "ALL":
        return {}
    out = {}
    for part in key.split(";"):
        f, vs = part.split("=", 1)
        out[f] = vs.split("|")
    return out


def candidate_segments(present: dict[str, list[str]] | None = None) -> list[dict]:
    """Segments we test for heterogeneous effects: every single value, plus contiguous
    two/three-value ranges for ordinal features. Only values actually present in the
    workflow's traffic are used (`present`), to keep the hypothesis family small."""
    out: list[dict] = []
    for f in FEAT_NAMES:
        vals = [v for v in FEATURES[f] if present is None or v in present.get(f, [])]
        if len(vals) < 2:
            continue
        for v in vals:
            out.append({f: [v]})
        if f in ORDINAL:
            for k in (2, 3):
                for i in range(len(vals) - k + 1):
                    if k < len(vals):
                        out.append({f: vals[i:i + k]})
    return out


def cohort_index(config: dict) -> tuple[list[str], np.ndarray]:
    """Evaluates a config's ordered cohorts over all codes, exactly as the dialler does.
    Returns (names, idx[252]) where names[0] == 'default'."""
    names = ["default"]
    idx = np.zeros(N_CODES, dtype=np.int16)
    free = np.ones(N_CODES, dtype=bool)
    for i, c in enumerate(config.get("cohorts") or []):
        m = free & mask(c.get("where"))
        idx[m] = i + 1
        free &= ~m
        names.append(c["id"])
    return names, idx


def dist_for(config: dict, cohort: str, dim: str) -> dict:
    a = config.get("allocations") or {}
    d = (a.get(cohort) or {}).get(dim) or a.get("default", {}).get(dim) or {}
    s = sum(d.values()) or 1.0
    return {k: v / s for k, v in d.items() if v > 0}


def effective_dists(config: dict, dim: str) -> list[dict]:
    """Per code: the distribution over `dim` options a lead with that code is dialled with."""
    names, idx = cohort_index(config)
    per_cohort = [dist_for(config, n, dim) for n in names]
    return [per_cohort[i] for i in idx.tolist()]


def quantize(dist: dict, q: int = 1024) -> dict:
    """Weights as exact multiples of 1/q (q a power of two) summing to exactly 1.0.

    The simulator re-normalises every distribution (w / sum). Dyadic weights sum exactly in
    floating point, so re-normalising them is a no-op: a dimension we did not mean to change can
    never look 'changed' because of float noise (which would, e.g., make an auto-apply of
    caller IDs touch a customer-facing dimension)."""
    tot = sum(v for v in dist.values() if v > 0)
    if tot <= 0:
        return {}
    raw = {k: v / tot * q for k, v in dist.items() if v > 0}
    base = {k: int(v) for k, v in raw.items()}
    rem = q - sum(base.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - base[k]), k))[:rem]:  # deterministic ties
        base[k] += 1
    return {k: v / q for k, v in base.items() if v > 0}


def exact_changed_dims(old: dict, new: dict) -> list[str]:
    """The simulator's own notion of a changed dimension (exact, after normalisation)."""
    def norm(d):
        s = float(sum(d.values()))
        return {o: float(w) / s for o, w in d.items() if w > 0}

    def dist(cfg, k, dim):
        a = cfg.get("allocations") or {}
        return norm((a.get(k) or {}).get(dim) or a["default"][dim])
    ch = []
    if (old.get("cohorts") or []) != (new.get("cohorts") or []):
        ch.append("cohorts")
    keys = set(old.get("allocations") or {}) | set(new.get("allocations") or {})
    for dim in ("welcome_message", "voice", "caller_id"):
        if any(dist(old, k, dim) != dist(new, k, dim) for k in keys):
            ch.append(dim)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.append("prompt_version")
    return ch
