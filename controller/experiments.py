"""Welcome-message x voice experimentation: exploration, winner selection, harm stops, decay rollback.

Design (see SUBMISSION.md section 5 for the reasoning and the measured false-winner rate):
  * Factorial by construction: the dialler samples welcome and voice independently, so one
    allocation explores both dimensions at once. Comparisons are stratified by the *other*
    dimension, so an interaction cannot masquerade as a main effect.
  * Control = the incumbent option (the business default, or whatever a supervisor last chose).
    Each challenger gets 10% of traffic (30% cap per dim). After a promotion the control keeps a
    20% holdout forever: that holdout is what lets us notice a winner decaying.
  * One "look" per day per workflow (the daily plan). Promotion = Holm-Bonferroni over every
    (dim, arm, segment) hypothesis at that look: alpha 0.02 for whole-population hypotheses and
    0.01 for segment hypotheses (Bonferroni split, FWER <= 0.03 per look), min 300 connected
    calls per side, and a point lift of at least +2%.
  * Harm stop: any (arm, segment) with z <= -2.58 on >= 150 connected calls is pulled from that
    segment immediately. No multiplicity correction on purpose: a false harm flag costs us some
    exploration; a missed one costs a client's customers.
  * Decay: every 6 h, each live rule is re-tested against the holdout on the last ~2 days only.
    z <= -1.96 on >= 200 holdout calls -> rollback.
"""
from __future__ import annotations

import numpy as np

from . import stats
from .features import FEATURES, candidate_segments, mask, seg_key

DIMS = ("welcome_message", "voice")
AXIS = {"welcome_message": 2, "voice": 3}  # axis in eng cube [day, code, W, V, P, 3]

ALPHA_ALL, ALPHA_SEG = 0.02, 0.01
MIN_N = 300
MIN_LIFT = 0.02
HARM_Z, HARM_MIN_N = -2.58, 150
DECAY_Z = -1.96
FUTILITY_N = 2500
PROMO_WINDOW_DAYS = 4
DECAY_WINDOW_DAYS = 2
MAX_SEG_RULES_PER_DIM = 3
SUPERVISOR_PENALTY = 10
SOFT_STOP_Z, SOFT_STOP_LIFT = -1.645, 0.05   # stop *exploring* an arm trending >5% worse
MAX_EXPLORE_DAYS = 6


def init_state(w, config: dict) -> dict:
    st = {}
    for d in DIMS:
        opts = w.opts[d]
        dist = config["allocations"]["default"].get(d, {})
        ctl = max(dist, key=dist.get) if dist else opts[0]
        st[d] = {"control": ctl, "active": [o for o in opts if o != ctl], "rules": [], "harm": [], "decayed": [],
                 "dropped": []}
    return st


def present_values(w, today: int) -> dict[str, list[str]]:
    """Feature values with >= 3% of the workflow's recent connected calls."""
    E = w.days("eng", today - 7, today)
    out = {f: [] for f in FEATURES}
    if E is None:
        return out
    n_code = E[..., 0].reshape(E.shape[0], -1).sum(axis=1)
    tot = n_code.sum() or 1
    for f, vals in FEATURES.items():
        for v in vals:
            if n_code[mask({f: [v]})].sum() / tot >= 0.03:
                out[f].append(v)
    return out


def compare(E: np.ndarray, dim: str, arm_i: int, ctl_i: int, seg_mask: np.ndarray) -> stats.Cmp | None:
    """E: eng cube stacked over days [day, code, W, V, P, 3]. Engagement rate arm vs control."""
    ax = AXIS[dim]
    sub = E[:, seg_mask]
    a = np.take(sub, arm_i, axis=ax)
    c = np.take(sub, ctl_i, axis=ax)
    return stats.mh(a[..., 1], a[..., 0], c[..., 1], c[..., 0])


def look(w, st: dict, today: int, minute: int, decay_only: bool = False) -> list[dict]:
    """Updates experiment state `st` in place from the data; returns audit events describing
    every state change and the evidence for it."""
    events: list[dict] = []
    Ep = w.stack("eng", today - PROMO_WINDOW_DAYS + 1, today)
    Ed = w.stack("eng", today - DECAY_WINDOW_DAYS + 1, today)
    if Ep is None:
        return events
    segs = [{}] + ([] if decay_only else candidate_segments(present_values(w, today)))

    # ---- decay / stopped-winning checks on live rules (recent window only)
    for d in DIMS:
        s = st[d]
        ctl_i = w.oi[d][s["control"]]
        for r in list(s["rules"]):
            c = compare(Ed, d, w.oi[d][r["arm"]], ctl_i, mask(r["seg"])) if Ed is not None else None
            if c is None or c.n_ctl < 100:
                continue
            r["last_check"] = c.as_dict()
            if c.z <= DECAY_Z and c.n_ctl >= 200:
                s["rules"].remove(r)
                s["decayed"].append({"seg": r["seg"], "arm": r["arm"], "minute": minute})
                events.append({"kind": "decay_rollback", "dim": d, "arm": r["arm"], "seg": seg_key(r["seg"]),
                               "control": s["control"], "evidence": c.as_dict(),
                               "summary": f"{d}={r['arm']} stopped beating {s['control']} in {seg_key(r['seg'])} "
                                          f"(last {DECAY_WINDOW_DAYS}d lift {c.rr - 1:+.1%}, z={c.z:.2f}) -> roll back"})
    if decay_only:
        return events

    # ---- harm stops, promotion hypotheses, futility
    hyp = []  # (family, dim, arm, seg, cmp)
    for d in DIMS:
        s = st[d]
        if s["active"]:
            s.setdefault("explore_since", minute)
        ctl_i = w.oi[d][s["control"]]
        ruled = {(seg_key(r["seg"]), r["arm"]) for r in s["rules"]}
        harmed = {(seg_key(h["seg"]), h["arm"]) for h in s["harm"]}
        decayed = {(seg_key(x["seg"]), x["arm"]) for x in s["decayed"]}
        for arm in w.opts[d]:
            if arm == s["control"] or arm in s["dropped"]:
                continue
            # an arm not live anywhere (e.g. exploration frozen by a supervisor) needs no harm stop;
            # its old data can still prove it better than the control
            live = arm in s["active"] or any(r["arm"] == arm for r in s["rules"])
            for seg in segs:
                k = (seg_key(seg), arm)
                if k in harmed or k in decayed:
                    continue
                c = compare(Ep, d, w.oi[d][arm], ctl_i, mask(seg))
                if c is None:
                    continue
                # harm is checked on the recent window too: an arm that was fine and turned bad
                # (decay while still being explored) must not hide behind its good old data
                cr = compare(Ed, d, w.oi[d][arm], ctl_i, mask(seg)) if Ed is not None else None
                if cr is not None and cr.z <= HARM_Z and cr.n_arm >= HARM_MIN_N and (c.z > HARM_Z or
                                                                                     c.n_arm < HARM_MIN_N):
                    c = cr
                if live and c.z <= HARM_Z and c.n_arm >= HARM_MIN_N and k not in ruled:
                    s["harm"].append({"seg": seg, "arm": arm, "minute": minute, "evidence": c.as_dict()})
                    n_seg_harm = sum(1 for h in s["harm"] if h["arm"] == arm and h["seg"])
                    in_rule = any(r["arm"] == arm for r in s["rules"])
                    # One bad segment is a carve-out; two or more means the arm is simply not good
                    # enough to keep exploring anywhere -- drop it instead of fragmenting cohorts.
                    if (not seg or (n_seg_harm >= 2 and not in_rule)) and arm not in s["dropped"]:
                        if arm in s["active"]:
                            s["active"].remove(arm)
                        s["dropped"].append(arm)
                        if seg:
                            s["harm"] = [h for h in s["harm"] if h["arm"] != arm or not h["seg"]] + \
                                        [{"seg": {}, "arm": arm, "minute": minute, "evidence": c.as_dict(),
                                          "via": "multiple segment harms"}]
                    events.append({"kind": "harm_stop", "dim": d, "arm": arm, "seg": k[0], "evidence": c.as_dict(),
                                   "summary": f"{d}={arm} is worse than {s['control']} in {k[0]} "
                                              f"({c.rr - 1:+.1%}, z={c.z:.2f}, n={c.n_arm}) -> stop it there"})
                    continue
                if k in ruled:
                    continue
                if c.n_arm >= MIN_N and c.n_ctl >= MIN_N:
                    hyp.append(("ALL" if not seg else "SEG", d, arm, seg, c))
                exploring = not seg and arm in s["active"] and not any(r["arm"] == arm for r in s["rules"])
                why = None
                if exploring and c.n_arm >= FUTILITY_N and c.ci(1.96)[1] < 1 + MIN_LIFT:
                    why = f"{c.n_arm} calls, 95% CI upper {c.ci(1.96)[1] - 1:+.1%} < +{MIN_LIFT:.0%}"
                elif exploring and cr is not None and cr.z <= SOFT_STOP_Z and cr.rr < 1 - SOFT_STOP_LIFT \
                        and cr.n_arm >= HARM_MIN_N:
                    # Stopping exploration is cheap to get wrong (we only lose information), so an arm
                    # that is merely *trending* worse recently is stopped on much weaker evidence than
                    # a harm verdict. This is what catches an explored arm that decays.
                    c = cr
                    why = f"trending worse over the last {DECAY_WINDOW_DAYS}d ({cr.rr - 1:+.1%}, z={cr.z:.2f})"
                elif exploring and minute - s.get("explore_since", minute) >= MAX_EXPLORE_DAYS * 1440 \
                        and c.p_better > 0.2:
                    why = f"no sign of a win after {MAX_EXPLORE_DAYS} days ({c.rr - 1:+.1%}, z={c.z:.2f})"
                if why:
                    s["active"].remove(arm)
                    s["dropped"].append(arm)
                    events.append({"kind": "futility_stop", "dim": d, "arm": arm, "seg": "ALL",
                                   "evidence": c.as_dict(), "summary": f"{d}={arm}: {why} -> stop exploring"})

    for fam, alpha in (("ALL", ALPHA_ALL), ("SEG", ALPHA_SEG)):
        hs = [h for h in hyp if h[0] == fam]
        if not hs:
            continue
        # Overriding a supervisor's explicit choice needs 10x stronger evidence: their pick is a prior,
        # and being wrong against it costs trust, not just engagement. (Implemented by inflating the
        # p-value fed to Holm, which keeps the family-wise guarantee intact.)
        pv = [min(1.0, h[4].p_better * (SUPERVISOR_PENALTY if st[h[1]].get("frozen_by_supervisor") is not None else 1))
              for h in hs]
        acc = stats.holm(pv, alpha)
        for ok, (_, d, arm, seg, c) in sorted(zip(acc, hs), key=lambda t: -t[1][4].log_rr):
            if not ok or c.rr < 1 + MIN_LIFT:
                continue
            s = st[d]
            # Replacing an existing winner needs a head-to-head win, not just a win over control:
            # two arms that both beat control are not ordered by their separate lifts.
            incumbents = [r for r in s["rules"] if _overlap(r["seg"], seg) and r["arm"] != arm]
            beaten = True
            for r in incumbents:
                h2h = compare(Ep, d, w.oi[d][arm], w.oi[d][r["arm"]], mask(seg))
                if h2h is None or h2h.p_better > alpha / len(hs) or h2h.rr < 1 + MIN_LIFT:
                    beaten = False
                    break
            if not beaten:
                events.append({"kind": "winner_not_promoted", "dim": d, "arm": arm, "seg": seg_key(seg),
                               "evidence": c.as_dict(),
                               "summary": f"{d}={arm} beats {s['control']} in {seg_key(seg)} ({c.rr - 1:+.1%}) but not "
                                          f"the current winner {incumbents[0]['arm']} head-to-head: keep current"})
                continue
            if seg:
                # redundant if the same arm already wins for everyone; respect per-dim rule budget
                if any(not r["seg"] and r["arm"] == arm for r in s["rules"]):
                    continue
                if sum(1 for r in s["rules"] if r["seg"]) >= MAX_SEG_RULES_PER_DIM:
                    continue
                # only one rule per (dim, segment-overlap): keep the larger lift
                if any(r["seg"] and _overlap(r["seg"], seg) for r in s["rules"]):
                    continue
            lift = float(np.exp(stats.shrink(c.log_rr, c.se)) - 1)
            s["rules"].append({"seg": seg, "arm": arm, "lift": round(lift, 4), "minute": minute,
                               "evidence": c.as_dict(), "weak_looks": 0})
            events.append({"kind": "winner", "dim": d, "arm": arm, "seg": seg_key(seg), "evidence": c.as_dict(),
                           "family": fam, "alpha": alpha, "n_hypotheses": len(hs), "shrunk_lift": round(lift, 4),
                           "summary": f"{d}={arm} beats {s['control']} in {seg_key(seg)}: {c.rr - 1:+.1%} "
                                      f"(90% CI {c.ci()[0] - 1:+.1%}..{c.ci()[1] - 1:+.1%}, z={c.z:.2f}, "
                                      f"Holm over {len(hs)} at alpha={alpha})"})
    return events


def _overlap(a: dict, b: dict) -> bool:
    for f in set(a) & set(b):
        if not set(a[f]) & set(b[f]):
            return False
    return True


def set_control(st: dict, dim: str, new_ctl: str, opts: list[str], minute: int) -> dict | None:
    """A supervisor chose an option for everyone: it becomes the incumbent. Learned harm/decay
    facts stay (they are about arms, not about the control); rules are dropped because their
    lift was measured against the old control."""
    s = st[dim]
    if s["control"] == new_ctl:
        return None
    old = s["control"]
    s["control"] = new_ctl
    s["rules"] = []
    # We stop *exploring* this dimension: the supervisor made a call, and new exploration would
    # spend their customers on arms they just chose against. Data already collected stays valid,
    # so an arm that later proves significantly better than their choice can still be proposed
    # (with that evidence, through approval).
    s["active"] = []
    s["frozen_by_supervisor"] = minute
    return {"kind": "control_changed", "dim": dim, "from": old, "to": new_ctl,
            "summary": f"supervisor set {dim}={new_ctl} for everyone: it is the new control, exploration on "
                       f"{dim} stops; rules measured against {old} are retired"}


def expected_mult(config: dict, st: dict, w, codes_weight: np.ndarray) -> float:
    """Model-based expected engagement multiplier of a config relative to all-control, averaged
    over leads weighted by `codes_weight` (e.g. recent connected calls per code). Uses only
    accepted rules (shrunk lifts); unproven arms are assumed equal to control (their true value is
    unknown -- this is the honest prior, and is why exploration shows ~0 expected impact)."""
    from .features import effective_dists
    tot = codes_weight.sum()
    if tot <= 0:
        return 1.0
    m = np.ones(len(codes_weight))
    for d in DIMS:
        dists = effective_dists(config, d)
        s = st[d]
        lifts = np.zeros(len(codes_weight))
        for code, dist in enumerate(dists):
            if codes_weight[code] == 0:
                continue
            v = 0.0
            for arm, p in dist.items():
                v += p * _arm_lift(s, arm, code)
            lifts[code] = v
        m *= lifts
    return float((m * codes_weight).sum() / tot)


def _arm_lift(s: dict, arm: str, code: int) -> float:
    if arm == s["control"]:
        return 1.0
    best = None
    for r in s["rules"]:
        if r["arm"] == arm and mask(r["seg"])[code]:
            best = max(best or 0, 1 + r["lift"])
    for x in s.get("decayed", []):
        if x["arm"] == arm and mask(x["seg"])[code]:
            return 0.97  # decayed: we believe it is now slightly worse
    for h in s.get("harm", []):
        if h["arm"] == arm and mask(h["seg"])[code]:
            ev = h.get("evidence", {})
            return 1 + min(ev.get("lift", -0.05), 0)
    return best if best is not None else 1.0
