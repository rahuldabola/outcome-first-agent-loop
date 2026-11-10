"""Read-only views over the controller's store. Nothing here talks to the simulator.

  view    the one screen a client supervisor asked for (C11)
  why     "why did workflow X change at T, who approved it, what happened next" (C12)
  log     the raw decision log
  health  the loop's own health: ingest rate, lag, memory, credits, alerts (C14)
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

import numpy as np

from . import experiments as X
from .features import seg_key
from .ingest import WfData
from .store import Store

MIN_PER_DAY = 1440


def fmt(m) -> str:
    if m is None:
        return "-"
    d, r = divmod(int(m), MIN_PER_DAY)
    return f"D{d} {r // 60:02d}:{r % 60:02d}"


def parse_at(s: str) -> int:
    s = str(s).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    day, hm = s.split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * MIN_PER_DAY + int(h) * 60 + int(m)


def _wfdata(st: Store, wf: str | None = None) -> dict[str, WfData]:
    specs = st.get("specs") or {}
    out = {wid: WfData(s) for wid, s in specs.items() if wf in (None, wid)}
    for wid, kind, day, arr in st.load_cubes():
        if wid in out:
            getattr(out[wid], kind)[day] = arr
    return out


def _kpi(w: WfData, lo: int, hi: int) -> dict:
    M = w.days("mon", lo, hi)
    if M is None:
        return {}
    dials, conn, eng = M[..., 0].sum(), M[..., 1].sum(), M[..., 2].sum()
    return {"dials": int(dials), "pickup": conn / dials if dials else None, "engagement": eng / conn if conn else None}


def _pct(x):
    return "-" if x is None else f"{x:.1%}"


# ---------------------------------------------------------------------------------------- view
def view(a) -> str:
    st = Store(a.db)
    last = st.q1("SELECT MAX(minute) AS m FROM metrics")["m"] or 0
    today = a.day if getattr(a, "day", None) is not None else last // MIN_PER_DAY
    wfs = _wfdata(st, a.workflow)
    specs = st.get("specs") or {}
    L = [f"# Supervisor view - {st.get('scenario')} - as of {fmt(last)}", ""]
    for wid, w in wfs.items():
        spec = specs[wid]
        obj = spec["objective"]
        L += [f"## {wid} ({spec['tenant_id']}) - primary: {obj['primary']}, guardrail: {json.dumps(obj.get('guardrail'))}"]
        t, y, base = _kpi(w, today, today), _kpi(w, today - 1, today - 1), _kpi(w, -7, -1)
        L += ["", "| | pickup | engagement | dials |", "|---|---|---|---|",
              f"| today D{today} | {_pct(t.get('pickup'))} | {_pct(t.get('engagement'))} | {t.get('dials', 0)} |",
              f"| yesterday | {_pct(y.get('pickup'))} | {_pct(y.get('engagement'))} | {y.get('dials', 0)} |",
              f"| last 7 days before D0 | {_pct(base.get('pickup'))} | {_pct(base.get('engagement'))} | "
              f"{base.get('dials', 0)} |", ""]
        cfg = st.q1("SELECT * FROM configs WHERE wf=? AND config IS NOT NULL ORDER BY version DESC LIMIT 1", (wid,))
        if cfg:
            c = json.loads(cfg["config"])
            d = c["allocations"]["default"]
            L += [f"**Live config v{cfg['version']}** (set by {cfg['actor']} at {fmt(cfg['minute'])}): script "
                  f"{c['prompt_version']}, {len(c['cohorts'])} cohorts, everyone-else gets welcome "
                  f"{_d(d.get('welcome_message'))}, voice {_d(d.get('voice'))}, caller ID {_d(d.get('caller_id'))}", ""]
        exp = st.get_state("exp_state", wid)
        if exp:
            E = w.stack("eng", today - X.PROMO_WINDOW_DAYS + 1, today)
            L += ["**What is being tested and what is winning** (engagement vs control, last 4 days, "
                  "stratified):", "", "| dimension | option | status | answered calls | engagement | vs control |",
                  "|---|---|---|---|---|---|"]
            for dim in X.DIMS:
                s = exp[dim]
                for arm in w.opts[dim]:
                    if arm == s["control"]:
                        status = "control (holdout)" if any(r["arm"] != arm for r in s["rules"]) else "control"
                    elif any(r["arm"] == arm for r in s["rules"]):
                        status = "WINNING in " + ", ".join(seg_key(r["seg"]) for r in s["rules"] if r["arm"] == arm)
                    elif arm in s.get("dropped", []):
                        status = "stopped"
                    elif arm in s.get("active", []):
                        status = "testing"
                    else:
                        status = "not tested"
                    extra = [f"stopped for {seg_key(h['seg'])}" for h in s.get("harm", [])
                             if h["arm"] == arm and h["seg"] and arm not in s.get("dropped", [])]
                    extra += [f"rolled back in {seg_key(x['seg'])}" for x in s.get("decayed", []) if x["arm"] == arm]
                    if extra:
                        status += "; " + "; ".join(extra)
                    row = f"| {dim} | {arm} | {status} |"
                    if E is not None and arm != s["control"]:
                        cmp = X.compare(E, dim, w.oi[dim][arm], w.oi[dim][s["control"]], np.ones(E.shape[1], bool))
                        if cmp:
                            lo, hi = cmp.ci()
                            row += f" {cmp.n_arm} | {cmp.rate_arm:.1%} | {cmp.rr - 1:+.1%} (90% CI {lo - 1:+.1%}..{hi - 1:+.1%}) |"
                        else:
                            row += " 0 | - | - |"
                    elif E is not None:
                        i = w.oi[dim][arm]
                        sub = np.take(E, i, axis=X.AXIS[dim])
                        n, e = int(sub[..., 0].sum()), int(sub[..., 1].sum())
                        row += f" {n} | {_pct(e / n if n else None)} | - |"
                    else:
                        row += " | | |"
                    L.append(row)
            L.append("")
        pend = st.q("SELECT * FROM proposals WHERE wf=? AND status IN ('pending','approved','applying','intent') "
                    "ORDER BY created_minute", (wid,))
        L.append("**Waiting on you:** " + ("nothing" if not pend else ""))
        for p in pend:
            b = json.loads(p["body"])["request"]
            L.append(f"- [{p['status']}] {p['approval_id'] or p['proposal_id']} (sent {fmt(p['created_minute'])}, "
                     f"impact {b.get('impact_score')}, {b.get('affected_leads')} leads): {b.get('change_summary')}")
        L.append("")
        lo = today * MIN_PER_DAY
        ch = st.q("SELECT * FROM configs WHERE wf=? AND minute>=? ORDER BY version", (wid, lo))
        L.append("**Changed today:** " + ("nothing" if not ch else ""))
        for c in ch:
            why = ""
            if c["proposal_id"]:
                p = st.q1("SELECT body FROM proposals WHERE proposal_id=?", (c["proposal_id"],))
                if p:
                    b = json.loads(p["body"])["request"]
                    why = b.get("change_summary") or b.get("rationale", "").split("\n")[0]
            L.append(f"- {fmt(c['minute'])} v{c['version']} by {c['actor']} ({', '.join(json.loads(c['changed_dims'] or '[]'))})"
                     f"{': ' + why if why else ''}")
        ws = st.get_state("wf_state", wid)
        did = ws.get("did", {}).get("status", {})
        if did:
            L += ["", "**Caller IDs:** " + ", ".join(f"{d} {s['status']}" for d, s in did.items())]
        inc = ws.get("incident")
        if inc and inc.get("status") != "closed":
            L += [f"**Open incident:** {inc['kind']} on {inc['bad']} since {fmt(inc['opened'])} - {inc['status']}"]
        al = st.q("SELECT * FROM alerts WHERE wf=? AND resolved_minute IS NULL AND severity!='info' ORDER BY id", (wid,))
        if al:
            L += ["", "**Needs a human:**"] + [f"- [{x['severity']}] {fmt(x['minute'])} {x['message']}" for x in al]
        L.append("\n---\n")
    return "\n".join(L)


def _d(dist):
    if not dist:
        return "-"
    return "/".join(f"{k} {v:.0%}" for k, v in sorted(dist.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------------------- why
def why(a) -> str:
    st = Store(a.db)
    T = parse_at(a.at)
    wid = a.workflow
    v = st.q1("SELECT * FROM configs WHERE wf=? AND (minute IS NULL OR minute<=?) ORDER BY version DESC LIMIT 1",
              (wid, T))
    if not v:
        return f"no config known for {wid} at {fmt(T)}"
    L = [f"# Why {wid} looked the way it did at {fmt(T)}", "",
         f"Config **v{v['version']}** was live, applied {fmt(v['minute'])} by **{v['actor']}** "
         f"(changed: {', '.join(json.loads(v['changed_dims'] or '[]')) or 'initial business default'})."]
    if v["actor"] == "supervisor":
        L.append("This was a client supervisor's edit (bypasses governance by design). Our reaction:")
        for d in st.q("SELECT * FROM decisions WHERE wf=? AND minute>=? AND minute<? ORDER BY id",
                      (wid, v["minute"], v["minute"] + MIN_PER_DAY)):
            if d["kind"] in ("supervisor_edit", "control_changed", "incident_opened", "prompt_screened",
                             "approval_requested", "applied", "incident_verified"):
                L.append(f"- {fmt(d['minute'])} {d['kind']}: {d['summary']}")
    if v["proposal_id"]:
        p = st.q1("SELECT * FROM proposals WHERE proposal_id=?", (v["proposal_id"],))
        if p:
            b = json.loads(p["body"])["request"]
            L += ["", f"## The change ({p['channel']}, proposal `{p['proposal_id']}`)",
                  f"- summary: {b.get('change_summary') or b.get('rationale', '').splitlines()[0]}",
                  f"- requested {fmt(p['created_minute'])}; "
                  + (f"approval {p['approval_id']} decided {fmt(p['decided_minute'])} by the client approver "
                     f"({(st.get('specs') or {}).get(wid, {}).get('tenant_id')}); "
                     if p["channel"] == "approval" else "auto-applied within the workflow's signed auto-apply policy; ")
                  + f"status **{p['status']}**, live as v{p['applied_version']}",
                  f"- impact score {b.get('impact_score')}, affected leads {b.get('affected_leads')}",
                  "", "Rationale:", "```", b.get("rationale", ""), "```"]
            if b.get("dry_run"):
                dr = {k: v_ for k, v_ in b["dry_run"].items() if k != "per_cohort"}
                L += ["Dry run:", "```", json.dumps(dr, indent=1), "```"]
            if b.get("diff"):
                L += ["Diff:", "```", json.dumps(b["diff"], indent=1)[:3000], "```"]
            ev = st.q("SELECT * FROM decisions WHERE wf=? AND minute=? AND kind IN "
                      "('winner','harm_stop','decay_rollback','futility_stop','incident_opened','caller_id_status')",
                      (wid, p["created_minute"]))
            if ev:
                L += ["Evidence recorded at proposal time:"] + [f"- {d['kind']}: {d['summary']}" for d in ev]
    L += ["", "## What happened next (24 h)"]
    for d in st.q("SELECT * FROM decisions WHERE wf=? AND minute>? AND minute<=? ORDER BY id",
                  (wid, T, T + MIN_PER_DAY)):
        if d["kind"] in ("plan_no_change", "spend_validation"):
            continue
        L.append(f"- {fmt(d['minute'])} {d['kind']}: {d['summary']}")
    wfs = _wfdata(st, wid)
    if wid in wfs and v["minute"] is not None:
        w = wfs[wid]
        day = v["minute"] // MIN_PER_DAY
        b4, af = _kpi(w, day - 2, day - 1), _kpi(w, day + 1, day + 2)
        L += ["", f"KPIs 2 days before vs 2 days after v{v['version']}: pickup {_pct(b4.get('pickup'))} -> "
                  f"{_pct(af.get('pickup'))}, engagement {_pct(b4.get('engagement'))} -> {_pct(af.get('engagement'))} "
                  f"(descriptive only: other changes overlap; experiment arms are the causal evidence)"]
    return "\n".join(L)


# ---------------------------------------------------------------------------------------- log
def log(a) -> str:
    st = Store(a.db)
    q, args = "SELECT * FROM decisions WHERE 1=1", []
    if a.workflow:
        q += " AND wf=?"
        args.append(a.workflow)
    if a.kind:
        q += " AND kind=?"
        args.append(a.kind)
    rows = st.q(q + " ORDER BY id DESC LIMIT ?", (*args, a.limit))
    return "\n".join(f"{fmt(r['minute'])}  {r['wf'] or '-':12s} {r['kind']:24s} {r['summary']}" for r in reversed(rows))


# ---------------------------------------------------------------------------------------- health
def health(a) -> str:
    st = Store(a.db)
    ms = [(r["minute"], json.loads(r["data"])) for r in st.q("SELECT * FROM metrics ORDER BY minute")]
    if not ms:
        return "no metrics yet"
    by_day = defaultdict(lambda: Counter())
    walls = defaultdict(list)
    for m, d in ms:
        day = m // MIN_PER_DAY
        by_day[day]["events"] += d["events"]
        by_day[day]["ingest_s"] += d["ingest_s"]
        by_day[day]["decide_s"] += d["decide_s"]
        by_day[day]["ticks"] += 1
        by_day[day]["rss_max"] = max(by_day[day]["rss_max"], d.get("rss_mb") or 0)
        by_day[day]["decide_max"] = max(by_day[day]["decide_max"], d["decide_s"])
        walls[day].append(d["wall"])
    L = [f"# Loop health - {st.get('scenario')}", "",
         "| sim day | events | ingest s | events/s | decide s (sum / worst tick) | wall s for the day | peak RSS MB |",
         "|---|---|---|---|---|---|---|"]
    days = sorted(by_day)
    for i, day in enumerate(days):
        c = by_day[day]
        nxt = walls[days[i + 1]][0] if i + 1 < len(days) else walls[day][-1]
        wall = nxt - walls[day][0]
        L.append(f"| D{day} | {c['events']} | {c['ingest_s']:.1f} | {c['events'] / c['ingest_s'] if c['ingest_s'] else 0:,.0f} "
                 f"| {c['decide_s']:.1f} / {c['decide_max']:.2f} | {wall:.0f} | {c['rss_max']:.0f} |")
    last = ms[-1][1]
    dec = st.q("SELECT kind, COUNT(*) AS n FROM decisions GROUP BY kind ORDER BY n DESC")
    hours = max((ms[-1][0] - max(ms[0][0], 0)) / 60, 1)
    L += ["", f"credits remaining {last['credits']}; restarts {st.get('restarts', 0)}; "
              f"events ingested {last['events_total']} of {last['last_event_id']} produced "
              f"(lag {last['last_event_id'] - last['events_total']} events)",
          f"decisions/hour (sim): {sum(r['n'] for r in dec) / hours:.1f}  ->  " +
          ", ".join(f"{r['kind']} {r['n']}" for r in dec[:12])]
    al = st.q("SELECT severity, code, wf, minute, message, resolved_minute FROM alerts ORDER BY id")
    sev = Counter((x["severity"], x["resolved_minute"] is None) for x in al)
    L += [f"alerts: " + ", ".join(f"{s}{' open' if o else ' resolved'} {n}" for (s, o), n in sorted(sev.items())), ""]
    for x in al:
        if x["severity"] == "page":
            L.append(f"- PAGE {fmt(x['minute'])} {x['wf']} {x['code']}: {x['message']} "
                     f"{'(resolved ' + fmt(x['resolved_minute']) + ')' if x['resolved_minute'] is not None else '(OPEN)'}")
    return "\n".join(L)
