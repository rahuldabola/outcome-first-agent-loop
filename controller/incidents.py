"""Hallucination and engagement-regression incident response (C8).

Detect (population test, never one call):
  flags among connected calls on the live prompt over the last <= 4 dialling hours vs the
  baseline flag rate of the cleanest established prompt (>= 2000 connected calls of history).
  Trigger when n >= 150, observed >= 2x baseline, and Poisson tail P(X >= x) < 1e-4.
  With ~12 tests/workflow/day that is ~1 false alarm per workflow per ~2 years.

Fix, cheapest evidence first:
  1. A previously-live prompt with production evidence (>= 2000 connected calls, normal flag rate,
     engagement not worse) -- usually "revert to what ran before the bad edit". No LLM call.
  2. Only if none exists: ask the LLM (3 credits) for alternatives. The LLM proposes; it never
     decides -- validation pass rate gates safety and live monitoring judges engagement.
  Validation: n=300 (3 credits). A prompt with a 1% true failure rate passes the 0.97 bar with
  probability ~0.999 at n=300, while a 2.5% one fails it about half the time.

Verify: after apply, the new prompt's flag rate must not be significantly above baseline on
>= 300 connected calls; otherwise the incident re-opens.
"""
from __future__ import annotations

import numpy as np

from . import stats

DETECT_HOURS = 4
MIN_N = 150
P_TRIGGER = 1e-4
VALIDATION_N = 300
MIN_PROD_N = 2000


def prompt_totals(w, today: int, days: int = 21) -> dict[str, dict]:
    M = w.days("mon", today - days, today)
    out = {}
    if M is None:
        return out
    for i, p in enumerate(w.opts["prompt_version"]):
        n, f, e = int(M[:, i, 1].sum()), int(M[:, i, 3].sum()), int(M[:, i, 2].sum())
        if n:
            out[p] = {"connected": n, "flags": f, "engaged": e, "flag_rate": f / n, "eng_rate": e / n}
    return out


def baseline_rate(totals: dict) -> float | None:
    est = [t["flag_rate"] for t in totals.values() if t["connected"] >= MIN_PROD_N]
    return min(est) if est else None


def recent_prompt(w, minute: int, prompt: str, hours: int = DETECT_HOURS, since: int | None = None):
    """(connected, flags, engaged) for `prompt` over the last `hours` dialling hours."""
    pi = w.opts["prompt_version"].index(prompt)
    n = f = e = got = 0
    for k in range(1, 72):
        t = minute // 60 - k
        if since is not None and t * 60 < since - 59:
            break
        d, h = divmod(t, 24)
        a = w.mon.get(d)
        if a is None or a[h, pi, 1] == 0:
            continue
        n += int(a[h, pi, 1]); f += int(a[h, pi, 3]); e += int(a[h, pi, 2])
        got += 1
        if got >= hours:
            break
    return n, f, e


def detect(w, minute: int, prompt: str, totals: dict, live_since: int | None) -> dict | None:
    p0 = baseline_rate(totals)
    if p0 is None:
        return None
    n, f, _ = recent_prompt(w, minute, prompt, since=live_since)
    if n < MIN_N:
        return None
    mu = n * p0
    p = stats.poisson_sf(f, mu)
    if f / n >= 2 * p0 and p < P_TRIGGER:
        return {"prompt": prompt, "connected": n, "flags": f, "rate": round(f / n, 4), "baseline": round(p0, 4),
                "p_value": p}
    return None


def verify(w, minute: int, prompt: str, totals: dict, since: int) -> str | None:
    """'ok' | 'bad' | None (not enough data yet)."""
    p0 = baseline_rate(totals)
    n, f, _ = recent_prompt(w, minute, prompt, hours=48, since=since)
    if p0 is None or n < 300:
        return None
    return "bad" if stats.poisson_sf(f, n * p0) < 0.001 and f / n > 1.5 * p0 else "ok"


def known_good(w, today: int, totals: dict, exclude: set[str], history: list[dict]) -> list[str]:
    """Previously-live prompts with production evidence, most recently live first."""
    p0 = baseline_rate(totals)
    if p0 is None:
        return []
    ok = {p for p, t in totals.items() if t["connected"] >= MIN_PROD_N and t["flag_rate"] <= 1.5 * p0}
    seen, out = set(), []
    for v in reversed(history):
        p = v["config"]["prompt_version"] if v.get("config") else None
        if p and p in ok and p not in exclude and p not in seen:
            out.append(p)
            seen.add(p)
    for p in sorted(ok - seen - exclude, key=lambda p: (-totals[p]["eng_rate"], p)):
        out.append(p)
    return out


def engagement_regression(w, today: int, cur: str, prev: str) -> stats.Cmp | None:
    """Stratified (day, code, welcome, voice) engagement of the live prompt vs the previous one over
    the last 3 days. Day strata only overlap on the switch day; the comparison is otherwise a
    before/after one, which is weaker evidence -- so the trigger bar is high."""
    E = w.stack("eng", today - 6, today)
    if E is None:
        return None
    ci, pi = w.opts["prompt_version"].index(cur), w.opts["prompt_version"].index(prev)
    a, c = E[..., ci, :], E[..., pi, :]
    # collapse day: allow before/after comparison within (code, welcome, voice)
    a, c = a.sum(axis=0), c.sum(axis=0)
    return stats.mh(a[..., 1], a[..., 0], c[..., 1], c[..., 0])
