"""Event ingestion: NDJSON chunks -> per-workflow count cubes + lead/feature join.

Exactly-once: a chunk's effects (cube increments, new leads, concluded flags, config/approval
bookkeeping) and the cursor `last_chunk` are committed in ONE SQLite transaction. A crash before
the commit means the chunk is re-read from the simulator on restart; a crash after means it is
skipped. Chunks are numbered globally and never change, so this is idempotent by construction.

Cubes (int32 counts, one array per workflow per sim day):
  eng  [code, welcome, voice, prompt, (connected, engaged, halluc_flag)]   -- connected calls only
  pick [hour, code, caller_id, (dials, connected, engaged)]
  mon  [hour, prompt, (dials, connected, engaged, halluc_flag)]
Everything the controller decides is computed from these + the lead table. Raw events are not
kept in memory. A day of the load scenario (12 workflows) is ~6 MB of cubes.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict

import numpy as np

from .features import N_CODES, encode

log = logging.getLogger("ctl.ingest")
MIN_PER_DAY = 1440


class WfData:
    def __init__(self, spec: dict):
        self.id = spec["workflow_id"]
        self.spec = spec
        o = spec["options"]
        self.opts = {d: list(o[d]) for d in ("welcome_message", "voice", "caller_id", "prompt_version")}
        self.oi = {d: {v: i for i, v in enumerate(vs)} for d, vs in self.opts.items()}
        self.W, self.V = len(self.opts["welcome_message"]), len(self.opts["voice"])
        self.D, self.P = len(self.opts["caller_id"]), len(self.opts["prompt_version"])
        self.eng: dict[int, np.ndarray] = {}
        self.pick: dict[int, np.ndarray] = {}
        self.mon: dict[int, np.ndarray] = {}
        self.leads: dict[str, tuple[int, str]] = {}      # lead_id -> (code, campaign_id), recent only
        self.campaigns: dict[str, dict] = {}             # campaign_id -> {day, active[252], leads[]}
        self.cadence_days = len(spec["cadence"])

    def cube(self, kind: str, day: int) -> np.ndarray:
        store = getattr(self, kind)
        a = store.get(day)
        if a is None:
            shape = {"eng": (N_CODES, self.W, self.V, self.P, 3),
                     "pick": (24, N_CODES, self.D, 3),
                     "mon": (24, self.P, 4)}[kind]
            a = store[day] = np.zeros(shape, dtype=np.int32)
        return a

    def days(self, kind: str, lo: int, hi: int) -> np.ndarray | None:
        """Sum of a cube over days lo..hi inclusive (None if no data)."""
        arrs = [a for d, a in getattr(self, kind).items() if lo <= d <= hi]
        if not arrs:
            return None
        return np.sum(arrs, axis=0, dtype=np.int64)

    def stack(self, kind: str, lo: int, hi: int) -> np.ndarray | None:
        ds = sorted(d for d in getattr(self, kind) if lo <= d <= hi)
        if not ds:
            return None
        return np.stack([getattr(self, kind)[d] for d in ds]).astype(np.int64)

    def active_by_code(self, today: int) -> np.ndarray:
        """Leads not yet concluded whose campaign is still inside its cadence."""
        out = np.zeros(N_CODES, dtype=np.int64)
        for c in self.campaigns.values():
            if today - c["day"] < self.cadence_days:
                out += c["active"]
        return out


class Ingestor:
    def __init__(self, store, specs: dict[str, dict]):
        self.store = store
        self.wf = {wid: WfData(s) for wid, s in specs.items()}
        self.origin: dt.date | None = None
        o = store.get("origin_date")
        if o:
            self.origin = dt.date.fromisoformat(o)
        self._daycache: dict[str, int] = {}
        self.events_total = store.get("events_total", 0)

    # ------------------------------------------------------------------ time
    def set_origin(self, sim_minute: int, ts: str):
        if self.origin is None:
            t = dt.datetime.fromisoformat(ts[:19]) - dt.timedelta(minutes=sim_minute)
            self.origin = t.date()
            self.store.put("origin_date", self.origin.isoformat())

    def day_of(self, ts: str) -> int:
        d = self._daycache.get(ts[:10])
        if d is None:
            d = self._daycache[ts[:10]] = (dt.date.fromisoformat(ts[:10]) - self.origin).days
        return d

    def minute_of(self, ts: str) -> int:
        return self.day_of(ts) * MIN_PER_DAY + int(ts[11:13]) * 60 + int(ts[14:16])

    # ------------------------------------------------------------------ restore
    def restore(self, today: int):
        for wid, kind, day, arr in self.store.load_cubes():
            if wid in self.wf:
                getattr(self.wf[wid], kind)[day] = arr
        keep_from = today - 5
        for lead_id, wid, code, day, camp, concluded in self.store.x(
                "SELECT lead_id, wf, code, day, campaign, concluded FROM leads WHERE day >= ?", (keep_from,)):
            w = self.wf.get(wid)
            if not w:
                continue
            c = w.campaigns.setdefault(camp, {"day": day, "active": np.zeros(N_CODES, np.int64), "leads": []})
            c["leads"].append(lead_id)
            w.leads[lead_id] = (code, camp)
            if not concluded and code >= 0:
                c["active"][code] += 1
        log.info("restored cubes + %d recent leads", sum(len(w.leads) for w in self.wf.values()))

    def prune(self, today: int):
        """Forget the lead->feature join for campaigns that can no longer be dialled."""
        for w in self.wf.values():
            for cid in [c for c, v in w.campaigns.items() if today - v["day"] > w.cadence_days + 1]:
                for lid in w.campaigns[cid]["leads"]:
                    w.leads.pop(lid, None)
                del w.campaigns[cid]

    # ------------------------------------------------------------------ ingestion
    def ingest_chunk(self, n: int, data: bytes, on_config_changed, on_approval_decided) -> int:
        """Parses one chunk and commits its effects + cursor atomically. Returns #events."""
        calls = defaultdict(list)          # wid -> [(day, hour, code, w, v, d, p, conn, eng, flag)]
        new_leads = []
        concluded = []
        touched: set[tuple[str, str, int]] = set()
        other = []
        nev = 0
        for line in data.splitlines():
            if not line:
                continue
            ev = json.loads(line)
            nev += 1
            t = ev["type"]
            if t == "call":
                w = self.wf.get(ev["workflow_id"])
                if w is None:
                    continue
                code, camp = w.leads.get(ev["lead_id"], (-1, None))
                v = ev["variant"]
                conn = ev["connected"]
                eng = ev["engaged"]
                flag = bool(conn and ev.get("qa") and ev["qa"].get("hallucination_flag"))
                ts = ev["ts"]
                calls[w.id].append((self.day_of(ts), int(ts[11:13]), code,
                                    w.oi["welcome_message"].get(v["welcome_message"], 0),
                                    w.oi["voice"].get(v["voice"], 0),
                                    w.oi["caller_id"].get(v["caller_id"], 0),
                                    w.oi["prompt_version"].get(v["prompt_version"], 0),
                                    conn, eng, flag))
                if eng and camp is not None:
                    c = w.campaigns.get(camp)
                    if c is not None and code >= 0:
                        c["active"][code] -= 1
                    concluded.append((ev["lead_id"],))
            elif t == "lead":
                w = self.wf.get(ev["workflow_id"])
                if w is None:
                    continue
                code = encode(ev["features"])
                camp = ev["campaign_id"]
                day = self.day_of(ev["ts"])
                c = w.campaigns.setdefault(camp, {"day": day, "active": np.zeros(N_CODES, np.int64), "leads": []})
                c["leads"].append(ev["lead_id"])
                if code >= 0:
                    c["active"][code] += 1
                w.leads[ev["lead_id"]] = (code, camp)
                new_leads.append((ev["lead_id"], w.id, code, day, camp))
            elif t == "campaign_created":
                w = self.wf.get(ev["workflow_id"])
                if w is not None:
                    w.campaigns.setdefault(ev["campaign_id"], {"day": self.day_of(ev["ts"]),
                                                               "active": np.zeros(N_CODES, np.int64), "leads": []})
            elif t in ("config_changed", "approval_decided"):
                other.append(ev)

        for wid, rows in calls.items():
            w = self.wf[wid]
            a = np.array(rows, dtype=np.int64)
            day, hour, code, wi, vi, di, pi, conn, eng, flag = a.T
            for d in np.unique(day).tolist():
                s = day == d
                pk = w.cube("pick", d)
                ok = s & (code >= 0)
                np.add.at(pk, (hour[ok], code[ok], di[ok], 0), 1)
                np.add.at(pk, (hour[ok], code[ok], di[ok], 1), conn[ok])
                np.add.at(pk, (hour[ok], code[ok], di[ok], 2), eng[ok])
                mo = w.cube("mon", d)
                np.add.at(mo, (hour[s], pi[s], 0), 1)
                np.add.at(mo, (hour[s], pi[s], 1), conn[s])
                np.add.at(mo, (hour[s], pi[s], 2), eng[s])
                np.add.at(mo, (hour[s], pi[s], 3), flag[s])
                c = ok & (conn == 1)
                ec = w.cube("eng", d)
                np.add.at(ec, (code[c], wi[c], vi[c], pi[c], 0), 1)
                np.add.at(ec, (code[c], wi[c], vi[c], pi[c], 1), eng[c])
                np.add.at(ec, (code[c], wi[c], vi[c], pi[c], 2), flag[c])
                for k in ("pick", "mon", "eng"):
                    touched.add((wid, k, d))

        st = self.store
        with st.tx():
            if new_leads:
                st.db.executemany("INSERT OR IGNORE INTO leads(lead_id, wf, code, day, campaign) VALUES (?,?,?,?,?)",
                                  new_leads)
            if concluded:
                st.db.executemany("UPDATE leads SET concluded=1 WHERE lead_id=?", concluded)
            for wid, kind, d in touched:
                st.put_cube(wid, kind, d, getattr(self.wf[wid], kind)[d])
            for ev in other:
                if ev["type"] == "config_changed":
                    on_config_changed(ev)
                else:
                    on_approval_decided(ev)
            self.events_total += nev
            st.put("events_total", self.events_total)
            st.put("last_chunk", n)
        return nev
