"""Measures the experiment engine's false-winner rate and power by Monte Carlo.

Feeds synthetic count cubes through the *real* `experiments.look()` once per simulated day, with
the same exploration split the controller uses (control 70%, three challengers at 10% each per
dimension; welcome x voice randomised independently).

  null   : every option identical             -> any 'winner' is a false winner
  effect : voice V2 is +X% for everyone       -> how many days until it is found
  segment: voice V2 is +15% for 20% of leads  -> segment discovery

  python scripts/aa_false_winner.py --runs 200 --connects 1000 --days 8
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import experiments as X  # noqa: E402
from controller.features import N_CODES, mask  # noqa: E402
from controller.ingest import WfData  # noqa: E402

SPEC = {"workflow_id": "wf", "tenant_id": "t", "dialling_window": [9, 21], "cadence": [2, 3, 3],
        "daily_batch": {"size": 1000}, "options": {"welcome_message": ["W1", "W2", "W3"],
                                                   "voice": ["V1", "V2", "V3", "V4"], "caller_id": ["D1"],
                                                   "prompt_version": ["P1"]}}
CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                                  "caller_id": {"D1": 1}}}, "prompt_version": "P1"}
TA = mask({"lang": ["ta"]})


def run(seed, connects, days, lift, seg_only):
    rng = np.random.default_rng(seed)
    w = WfData(SPEC)
    st = X.init_state(w, CFG)
    # realistic feature mix: lang hi/en/ta 50/30/20 etc. (ta = 20% of leads)
    p = np.ones(N_CODES)
    p[TA] *= 0.2 / TA.mean()
    p[~TA] *= 0.8 / (~TA).mean()
    p /= p.sum()
    pw = np.array([0.8, 0.1, 0.1])
    pv = np.array([0.7, 0.1, 0.1, 0.1])
    first = None
    for day in range(days):
        n = rng.poisson(connects)
        code = rng.choice(N_CODES, n, p=p)
        wi = rng.choice(3, n, p=pw)
        vi = rng.choice(4, n, p=pv)
        rate = np.full(n, 0.55) * np.exp(rng.normal(0, 0.15, n))
        if lift:
            hit = (vi == 1) & (TA[code] if seg_only else True)
            rate[hit] *= 1 + lift
        eng = rng.random(n) < np.minimum(rate, 0.95)
        E = w.cube("eng", day)
        np.add.at(E, (code, wi, vi, 0, 0), 1)
        np.add.at(E, (code, wi, vi, 0, 1), eng.astype(np.int32))
        ev = X.look(w, st, day, day * 1440 + 600)
        for e in ev:
            if e["kind"] == "winner" and first is None:
                first = (day, e["dim"], e["arm"], e["seg"])
    return first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--connects", type=int, default=1000, help="answered calls per day in the workflow")
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--out", default="reports/false_winner_rate.md")
    a = ap.parse_args()
    rows = []
    for name, lift, seg in (("null (A/A)", 0.0, False), ("V2 +10% for all", 0.10, False),
                            ("V2 +15% for all", 0.15, False), ("V2 +15% for lang=ta only (20%)", 0.15, True)):
        res = [run(1000 + i, a.connects, a.days, lift, seg) for i in range(a.runs)]
        found = [r for r in res if r is not None]
        right = [r for r in found if r[1] == "voice" and r[2] == "V2"]
        days = [r[0] for r in right]
        rows.append((name, len(found) / a.runs, len(right) / a.runs,
                     float(np.median(days)) if days else None,
                     sorted({r[3] for r in right})[:4]))
        print(rows[-1], flush=True)
    L = [f"# Experiment engine: false winners and power (Monte Carlo, {a.runs} runs each)", "",
         f"{a.connects} answered calls/day, control 70% + 3 challengers x 10% (voice) and 80/10/10 (welcome), "
         f"one look per day for {a.days} days, real `experiments.look()`.", "",
         "| truth | any winner declared | correct winner (V2) | median day found | segments it was found in |",
         "|---|---|---|---|---|"]
    for n, anyw, right, med, segs in rows:
        L.append(f"| {n} | {anyw:.1%} | {right:.1%} | {'-' if med is None else f'D{med:.0f}'} | {', '.join(segs) or '-'} |")
    L += ["", "Under the null, 'any winner declared' is the per-run false-winner rate across all "
              f"{a.days} looks and all (dim, arm, segment) hypotheses."]
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
