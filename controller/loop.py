"""The controller loop. One process, one thread, deterministic.

Each iteration (= one tick of `--step` simulated minutes, default 60):
  1. ingest every chunk after our cursor          (exactly-once, see ingest.py)
  2. reconcile/apply approvals that were decided  (governance.py)
  3. per workflow: caller-ID health, incident response, decay checks, daily plan
  4. record loop metrics
  5. POST /sim/advance

Restart = run the same code: boot() restores cubes + recent leads, reconciles every in-flight
proposal against the simulator, and the loop continues from the simulator's current minute.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import time
from pathlib import Path

from . import callerid, cohorts, experiments as X, incidents as I, planner, schedule, stats
from .client import ApiErr, Sim
from .features import dist_for as cohorts_dist, exact_changed_dims, seg_key
from .governance import Governance
from .ingest import Ingestor
from .store import Store, dumps

log = logging.getLogger("ctl.loop")
MIN_PER_DAY = 1440
CUSTOMER_FACING = {"welcome_message", "voice", "prompt_version"}


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.WorkingSetSize / 2 ** 20
    except Exception:
        return float("nan")


class Controller:
    def __init__(self, url: str, db: str, step: int = 60, prefs: str | None = None, stop_at: int | None = None):
        self.sim = Sim(url)
        self.st = Store(db)
        self.step = step
        self.stop_at = stop_at  # for crash tests: exit (as if killed) once sim minute >= stop_at
        p = Path(prefs) if prefs else Path(__file__).with_name("prefs.json")
        self.prefs = json.loads(p.read_text()) if p.exists() else {}
        self.now = 0
        self.cfg: dict[str, dict] = {}

    # ================================================================== boot
    def boot(self):
        self.sim.wait_ready()
        state = self.sim.get("/sim/state")
        self.specs = {w["workflow_id"]: w for w in self.sim.get("/workflows")}
        with self.st.tx():
            if self.st.get("scenario") not in (None, state["scenario"]):
                raise SystemExit(f"store {self.st.path} belongs to scenario {self.st.get('scenario')}, "
                                 f"simulator runs {state['scenario']}: use a fresh --db")
            self.st.put("scenario", state["scenario"])
            self.st.put("specs", self.specs)
            self.ing = Ingestor(self.st, self.specs)
            self.ing.set_origin(state["sim_minute"], state["ts"])
        self.now = state["sim_minute"]
        self.ing.restore(self.now // MIN_PER_DAY)
        self.gov = Governance(self.st, self.sim, self.specs, self.prefs)
        restarts = self.st.get("restarts", -1) + 1
        with self.st.tx():
            self.st.put("restarts", restarts)
            if restarts:
                self.st.decide(self.now, None, "controller_restarted",
                               f"controller restart #{restarts} at sim minute {self.now}",
                               {"last_chunk": self.st.get("last_chunk")})
        self.gov.reconcile(self.now)
        log.info("boot: scenario=%s minute=%s workflows=%d restarts=%d", state["scenario"], self.now,
                 len(self.specs), restarts)

    # ================================================================== main loop
    def run(self):
        self.boot()
        while True:
            t0 = time.time()
            state = self.sim.get("/sim/state")
            self.now = state["sim_minute"]
            nev = self.ingest_new()
            t_ing = time.time() - t0
            if not self.st.get("bootstrapped"):
                self.bootstrap()
            t1 = time.time()
            self.tick(state)
            t_dec = time.time() - t1
            self.metrics(state, nev, t_ing, t_dec)
            if state["finished"]:
                log.info("scenario finished at minute %s", self.now)
                with self.st.tx():
                    self.st.decide(self.now, None, "run_finished", "scenario finished", {})
                return
            if self.stop_at is not None and self.now >= self.stop_at:
                log.warning("stop_at reached (minute %s): exiting without cleanup", self.now)
                os._exit(17)
            self.sim.post("/sim/advance", {"minutes": self.step})

    def ingest_new(self) -> int:
        last = self.st.get("last_chunk", -1)
        chunks = self.sim.get("/events/chunks", kind="history", after=last) + \
            self.sim.get("/events/chunks", kind="events", after=last)
        n = 0
        for ch in sorted(chunks, key=lambda c: c["chunk"]):
            data = self.sim.chunk_text(ch["chunk"])
            n += self.ing.ingest_chunk(ch["chunk"], data, self.on_config_changed,
                                       lambda ev: self.gov.on_decided(ev, self.ing.minute_of(ev["ts"])))
        if n:
            self.ing.prune(self.now // MIN_PER_DAY)
        return n

    # ---- event hooks (run inside the ingest transaction)
    def on_config_changed(self, ev: dict):
        m = self.ing.minute_of(ev["ts"])
        # upsert: the row may already exist (with the config body) from a GET right after our own apply
        self.st.x("INSERT INTO configs(wf, version, actor, minute, changed_dims, proposal_id, approval_id) "
                  "VALUES (?,?,?,?,?,?,?) ON CONFLICT(wf, version) DO UPDATE SET actor=excluded.actor, "
                  "minute=excluded.minute, changed_dims=excluded.changed_dims, proposal_id=excluded.proposal_id, "
                  "approval_id=excluded.approval_id",
                  (ev["workflow_id"], ev["version"], ev["actor"], m, dumps(ev["changed_dims"]), ev.get("proposal_id"),
                   ev.get("approval_id")))
        if ev["actor"] == "supervisor" and m >= 0:
            ws = self.st.get_state("wf_state", ev["workflow_id"])
            ws["supervisor_cooldown_until"] = m + MIN_PER_DAY
            ws.setdefault("supervisor_edits", []).append({"minute": m, "version": ev["version"],
                                                         "dims": ev["changed_dims"]})
            self.st.put_state("wf_state", ev["workflow_id"], ws)
            self.st.decide(m, ev["workflow_id"], "supervisor_edit",
                           f"supervisor changed {', '.join(ev['changed_dims'])} (v{ev['version']}); routine "
                           f"proposals paused 24h, nothing of theirs is overwritten without an approval",
                           {"version": ev["version"], "changed_dims": ev["changed_dims"]})
            self.st.alert(m, ev["workflow_id"], "info", f"supervisor_edit_v{ev['version']}",
                          f"supervisor edit v{ev['version']}: {ev['changed_dims']}")

    # ================================================================== bootstrap (C3)
    def bootstrap(self):
        """Runs once, after all pre-D0 history is ingested: learn from history before acting."""
        with self.st.tx():
            for wid, w in self.ing.wf.items():
                cur = self.sim.get(f"/workflows/{wid}/config")
                exp = self.st.get_state("exp_state", wid) or X.init_state(w, cur["config"])
                self.st.put_state("exp_state", wid, exp)
                hist_days = [d for d in w.pick if d < 0]
                sch = schedule.fit(w, min(hist_days), max(hist_days)) if hist_days else None
                ws = self.st.get_state("wf_state", wid)
                ws["schedule"] = sch
                ws.setdefault("did", {})
                self.st.put_state("wf_state", wid, ws)
                tot = I.prompt_totals(w, -1, 30)
                sch_txt = (f"by {sch['feature']} (est. pickup {sch['estimate']:+.1%}, upper bound)"
                           if sch else "not worth changing")
                base_txt = ", ".join(f"{p}: {t['flag_rate']:.2%}" for p, t in tot.items())
                self.st.decide(self.now, wid, "bootstrap",
                               f"history: {len(hist_days)} days; call-time weights {sch_txt}; "
                               f"halluc-flag baseline {base_txt}",
                               {"schedule": sch, "prompt_totals": tot,
                                "controls": {d: exp[d]["control"] for d in X.DIMS}})
            self.st.put("bootstrapped", True)
        log.info("bootstrapped from history")

    # ================================================================== tick
    def tick(self, state: dict):
        applied = self.gov.apply_approved(self.now)
        for a in applied:
            log.info("apply %s -> %s", a["proposal_id"], a["status"])
        self.credits = state["credits_remaining"]
        for wid in self.specs:
            self.cfg[wid] = self.fetch_config(wid)
        for wid, w in self.ing.wf.items():
            try:
                self.sync_supervisor(wid, w)
                self.caller_ids(wid, w)
                self.incident(wid, w)
                if self.now % 360 == 0:
                    self.decay_check(wid, w)
                self.plan(wid, w)
            except ApiErr as e:
                log.error("%s: api error %s", wid, e)
                with self.st.tx():
                    self.st.alert(self.now, wid, "ticket", f"api_{e.code}", str(e)[:300])

    def fetch_config(self, wid: str) -> dict:
        c = self.sim.get(f"/workflows/{wid}/config")
        with self.st.tx():
            self.st.x("INSERT OR IGNORE INTO configs(wf, version, actor, minute, changed_dims) VALUES (?,?,?,?,?)",
                      (wid, c["version"], c["actor"], self.ing.minute_of(c["applied_at"]) if c["applied_at"] else None,
                       "[]"))
            self.st.x("UPDATE configs SET config=? WHERE wf=? AND version=? AND config IS NULL",
                      (dumps(c["config"]), wid, c["version"]))
            ws = self.st.get_state("wf_state", wid)
            if ws.get("prompt") != c["config"]["prompt_version"]:
                ws["prev_prompt"] = ws.get("prompt")
                ws["prompt"] = c["config"]["prompt_version"]
                ws["prompt_since"] = self.ing.minute_of(c["applied_at"]) if c["applied_at"] else None
                self.st.put_state("wf_state", wid, ws)
        return c

    def sync_supervisor(self, wid, w):
        ws = self.st.get_state("wf_state", wid)
        edits = [e for e in ws.get("supervisor_edits", []) if not e.get("synced")]
        if not edits:
            return
        with self.st.tx():
            exp = self.st.get_state("exp_state", wid)
            cfg = self.cfg[wid]["config"]
            for e in edits:
                for d in X.DIMS:
                    if d in e["dims"]:
                        dist = cfg["allocations"]["default"].get(d, {})
                        top = max(dist, key=dist.get)
                        ev = X.set_control(exp, d, top, w.opts[d], self.now)
                        if ev:
                            self.st.decide(self.now, wid, ev["kind"], ev["summary"], ev)
                e["synced"] = True
            self.st.put_state("exp_state", wid, exp)
            self.st.put_state("wf_state", wid, ws)

    # ================================================================== caller IDs (C5)
    def caller_ids(self, wid, w):
        spec = self.specs[wid]
        lo, hi = spec["dialling_window"]
        hour = (self.now % MIN_PER_DAY) // 60
        if not (lo < hour <= hi) or self.now < 0:
            return
        cfg = self.cfg[wid]
        cur = cfg["config"]["allocations"]["default"]["caller_id"]
        ws = self.st.get_state("wf_state", wid)
        did = ws.setdefault("did", {})
        before = {d: s["status"] for d, s in did.get("status", {}).items()}
        health = callerid.assess(w, self.now, did, cur)
        tgt = callerid.target(health, cur)
        bad_live = [d for d, h in health.items() if h["status"] in ("flagged", "poor") and cur.get(d, 0) > 0]
        with self.st.tx():
            for d, h in health.items():
                if before.get(d) != h["status"]:
                    sev = "page" if h["status"] == "flagged" else "info"
                    self.st.decide(self.now, wid, "caller_id_status",
                                   f"caller ID {d}: {before.get(d, 'new')} -> {h['status']}"
                                   + (f" (pickup ratio {h.get('ratio')} vs other numbers, z={h.get('z')})"
                                      if "ratio" in h else ""), h)
                    if h["status"] == "flagged":
                        self.st.alert(self.now, wid, sev, f"did_flagged_{d}",
                                      f"caller ID {d} pickup at {h.get('ratio')}x of other numbers: likely "
                                      f"spam-flagged; controller is rotating it out")
            ws["did"] = did
            ws["did_health"] = {d: h.get("health", 1.0) for d, h in health.items()}
            self.st.put_state("wf_state", wid, ws)
        if not callerid.worth_it(cur, tgt, health):
            if not bad_live:
                with self.st.tx():
                    for d in w.opts["caller_id"]:
                        self.st.resolve(self.now, wid, f"did_flagged_{d}")
            return
        pol = spec["governance"]["auto_apply"]
        if pol.get("enabled") and "caller_id" in pol.get("allowed_dims", []):
            if not bad_live and self.now - ws.get("did_last_change", -10 ** 9) < 240:
                return
            if self.gov.pending(wid, ("did",)):
                return
            if not bad_live and self.gov.pending(wid):
                # an approval is in flight on this base version: changing the config now would make
                # it stale on apply and waste the approver's yes. Rebalancing can wait.
                return
            step = callerid.step_toward(cur, tgt, pol.get("max_shift", 0))
            if not bad_live and not callerid.worth_it(cur, step, health):
                return
            new = copy.deepcopy(cfg["config"])
            new["allocations"]["default"]["caller_id"] = step
            for k, dims in new["allocations"].items():   # cohort-level caller-id maps follow the default
                if k != "default" and "caller_id" in dims:
                    dims["caller_id"] = step
            dims = exact_changed_dims(cfg["config"], new)
            if not dims or set(dims) - {"caller_id"}:
                if dims:
                    with self.st.tx():
                        self.st.alert(self.now, wid, "ticket", "auto_scope",
                                      f"caller-ID auto-apply would also change {dims}; skipped")
                return
            why = (f"caller ID {', '.join(bad_live)} degraded -> rotate out" if bad_live
                   else "rebalance caller IDs toward an even split over healthy numbers (+5% probes for unproven)")
            body = {"proposal_id": self.gov.next_id(wid, "did", self.now), "base_version": cfg["version"],
                    "proposed_config": new,
                    "rationale": why + "\n" + json.dumps({d: {k: h.get(k) for k in ("status", "ratio", "z", "dials")}
                                                          for d, h in health.items()}),
                    "evidence": {"did_health": health, "from": cur, "to": step, "target": tgt}}
            r = self.gov.auto(self.now, wid, "did", body, {"target": tgt})
            if r.get("status") == "applied":
                self.cfg[wid] = self.fetch_config(wid)
                ws = self.st.get_state("wf_state", wid)
                ws["did_last_change"] = self.now
                with self.st.tx():
                    self.st.put_state("wf_state", wid, ws)
        elif bad_live and not self.gov.pending(wid, ("did_urgent",)) and self.gov.can_request(wid, self.now, True):
            new = copy.deepcopy(cfg["config"])
            new["allocations"]["default"]["caller_id"] = tgt
            self.request(wid, w, "did_urgent", new, urgent=True,
                         summary=f"Stop dialling from caller ID {', '.join(bad_live)} (pickup collapsed)",
                         rationale=f"Caller ID {', '.join(bad_live)} connects at "
                                   f"{', '.join(str(health[d].get('ratio')) for d in bad_live)}x the rate of the "
                                   f"workflow's other numbers over the last 6 dialling hours (stratified by hour and "
                                   f"lead profile). Moving its share to healthy numbers. Operational change only: "
                                   f"nothing the customer hears changes.",
                         evidence={"did_health": health})

    # ================================================================== incidents (C8)
    def incident(self, wid, w):
        if self.now < 0:
            return
        spec = self.specs[wid]
        cfg = self.cfg[wid]
        cp = cfg["config"]["prompt_version"]
        ws = self.st.get_state("wf_state", wid)
        inc = ws.get("incident")
        today = self.now // MIN_PER_DAY
        totals = I.prompt_totals(w, today)

        if inc is None or inc["status"] == "closed":
            det = I.detect(w, self.now, cp, totals, ws.get("prompt_since"))
            kind = "hallucination"
            if det is None and ws.get("prev_prompt") and ws.get("prompt_since") is not None \
                    and self.now - ws["prompt_since"] <= 3 * MIN_PER_DAY:
                c = I.engagement_regression(w, today, cp, ws["prev_prompt"])
                if c and c.n_arm >= 400 and c.rr < 0.92 and c.z <= -3.5:
                    det = {"prompt": cp, "vs": ws["prev_prompt"], **c.as_dict()}
                    kind = "engagement_regression"
            if det is None:
                det = self._screen_new_prompt(wid, w, ws, cfg, cp, totals)
                kind = "hallucination"
            if det is None:
                return
            inc = {"status": "open", "kind": kind, "bad": cp, "opened": self.now, "detection": det, "tried": [],
                   "validations": {}, "llm": None}
            ws["incident"] = inc
            with self.st.tx():
                self.st.put_state("wf_state", wid, ws)
                msg = self._what(kind, det, cp)
                self.st.decide(self.now, wid, "incident_opened", msg, det)
                self.st.alert(self.now, wid, "page", "incident", msg + " -- fix in progress, see `why`")

        if inc["status"] in ("open",) and cp != inc["bad"]:
            # someone (supervisor or an earlier approval) already moved off the bad prompt
            inc["status"], inc["fix"], inc["fix_since"] = "verifying", cp, ws.get("prompt_since") or self.now
        if inc["status"] == "open":
            self._incident_fix(wid, w, ws, inc, totals, today)
        elif inc["status"] == "proposed":
            row = self.st.q1("SELECT status FROM proposals WHERE proposal_id=?", (inc["proposal_id"],))
            if row and row["status"] == "applied":
                inc["status"], inc["fix_since"] = "verifying", self.now
            elif row and row["status"] in ("rejected", "expired", "lapsed", "stale", "invalid", "failed",
                                           "stale_submit"):
                inc["status"] = "open"
                with self.st.tx():
                    self.st.decide(self.now, wid, "incident_retry",
                                   f"fix proposal {inc['proposal_id']} ended {row['status']}; re-proposing", {})
        elif inc["status"] == "verifying":
            v = I.verify(w, self.now, inc.get("fix", cp), totals, inc["fix_since"]) \
                if inc["kind"] == "hallucination" else self._verify_engagement(w, today, inc)
            if v == "ok":
                inc["status"], inc["closed"] = "closed", self.now
                with self.st.tx():
                    self.st.decide(self.now, wid, "incident_verified",
                                   f"{inc.get('fix', cp)} verified clean after fix "
                                   f"({(self.now - inc['opened']) / 60:.1f} h after detection)", inc)
                    self.st.resolve(self.now, wid, "incident")
            elif v == "bad":
                inc["tried"].append(inc.get("fix", cp))
                inc["bad"], inc["status"] = cp, "open"
                with self.st.tx():
                    self.st.decide(self.now, wid, "incident_reopened", f"{cp} still bad after fix; trying next", {})
        ws["incident"] = inc
        with self.st.tx():
            self.st.put_state("wf_state", wid, ws)

    @staticmethod
    def _what(kind, det, prompt):
        if det.get("validation_run_id"):
            return (f"new script {prompt} fails the safety bar: validation {det['validation_run_id']} pass rate "
                    f"{det['pass_rate']:.3f} < {det['bar']} on n={det['n']}")
        if kind == "hallucination":
            return (f"hallucination flags on {prompt}: {det['flags']}/{det['connected']} answered calls = "
                    f"{det['rate']:.1%} vs {det['baseline']:.1%} normal (p={det['p_value']:.1e})")
        return f"engagement on {prompt} {det['lift']:+.1%} vs {det['vs']} (z={det['z']})"

    def _screen_new_prompt(self, wid, w, ws, cfg, cp, totals):
        """A prompt we did not choose went live (supervisor edit) and has no production track record:
        spend 3 credits to validate it now instead of waiting hours for live flags to accumulate.
        This is the one place credits buy the most: it turns a ~4 h detection into a ~0 h one."""
        screened = ws.setdefault("screened_prompts", [])
        if cfg["actor"] != "supervisor" or cp in screened:
            return None
        if totals.get(cp, {}).get("connected", 0) >= I.MIN_PROD_N:
            return None
        if not self._spend_ok(wid, 3):
            return None
        val = self._spend(wid, "validation", "/validation/runs", {"workflow_id": wid, "prompt_version": cp, "n": 300}, 3)
        screened.append(cp)
        with self.st.tx():
            self.st.put_state("wf_state", wid, ws)
        if val is None:
            return None
        bar = self.specs[wid]["governance"].get("min_validation_pass_rate", 0.97)
        if val["pass_rate"] >= bar:
            with self.st.tx():
                self.st.decide(self.now, wid, "prompt_screened",
                               f"supervisor's new script {cp} passes validation ({val['pass_rate']:.3f} >= {bar}); "
                               f"live monitoring continues", val)
            return None
        return {"prompt": cp, "validation_run_id": val["validation_run_id"], "pass_rate": val["pass_rate"],
                "n": val["n"], "bar": bar, "p_value": 0.0}

    def _verify_engagement(self, w, today, inc):
        cp = self.cfg[w.id]["config"]["prompt_version"]
        c = I.engagement_regression(w, today, cp, inc["bad"])
        if c is None or c.n_arm < 300:
            return None
        return "ok" if c.rr >= 0.97 else "bad"

    def _incident_fix(self, wid, w, ws, inc, totals, today):
        spec = self.specs[wid]
        if self.gov.pending(wid, ("incident",)):
            return
        if not self.gov.can_request(wid, self.now, urgent=True):
            with self.st.tx():
                self.st.alert(self.now, wid, "page", "incident_blocked_budget",
                              "incident fix ready but approval budget for 24h is used up")
            return
        hist = [{"config": json.loads(r["config"])} for r in
                self.st.q("SELECT config FROM configs WHERE wf=? AND config IS NOT NULL ORDER BY version", (wid,))]
        exclude = {inc["bad"], *inc["tried"]}
        cands = I.known_good(w, today, totals, exclude, hist)
        source = "production history"
        if not cands:
            if inc["llm"] is None:
                if not self._spend_ok(wid, 3):
                    return
                r = self._spend(wid, "llm", "/llm/suggest-fixes",
                                {"workflow_id": wid, "problem": f"{inc['kind']} on prompt {inc['bad']}: "
                                                                f"{json.dumps(inc['detection'])}"}, 3)
                inc["llm"] = [s["prompt_version"] for s in (r or {}).get("suggestions", [])]
            cands = [p for p in inc["llm"] or [] if p not in exclude]
            source = "LLM suggestion (unproven on engagement; monitored after apply)"
        if not cands:
            with self.st.tx():
                self.st.alert(self.now, wid, "page", "incident_no_candidate",
                              f"no safe prompt candidate for {wid}; needs a human")
            return
        cand = cands[0]
        val = inc["validations"].get(cand)
        if val is None:
            n = I.VALIDATION_N
            if not self._spend_ok(wid, math.ceil(n / 100)):
                return
            val = self._spend(wid, "validation", "/validation/runs",
                              {"workflow_id": wid, "prompt_version": cand, "n": n}, math.ceil(n / 100))
            if val is None:
                return
            inc["validations"][cand] = val
        if val["pass_rate"] < spec["governance"].get("min_validation_pass_rate", 0.97):
            inc["tried"].append(cand)
            with self.st.tx():
                self.st.decide(self.now, wid, "candidate_failed_validation",
                               f"{cand} pass rate {val['pass_rate']:.3f} < bar; next candidate", val)
            return
        new = copy.deepcopy(self.cfg[wid]["config"])
        new["prompt_version"] = cand
        det = inc["detection"]
        what = self._what(inc["kind"], det, inc["bad"])
        pid = self.request(wid, w, "incident", new, urgent=True,
                           summary=f"Switch script {inc['bad']} -> {cand}: {what}",
                           rationale=f"Incident opened {(self.now - inc['opened']) / 60:.1f} h ago: {what} "
                                     f"(population test p={det.get('p_value', 0):.1e}). Candidate {cand} chosen from "
                                     f"{source}; validation {val['validation_run_id']} pass rate {val['pass_rate']:.3f} "
                                     f"on n={val['n']}. If {inc['bad']} was a supervisor edit, this reverts it -- "
                                     f"with your approval only. After apply we verify the flag rate returns to normal.",
                           evidence={"validation_run_id": val["validation_run_id"]})
        if pid:
            inc["status"], inc["proposal_id"], inc["fix"] = "proposed", pid, cand

    def _spend_ok(self, wid, cost) -> bool:
        """Credits are shared by all workflows: one workflow may spend at most its fair share per day
        (max(12, remaining / #workflows)), so a noisy workflow cannot strand another's incident."""
        day = self.now // MIN_PER_DAY
        spent = self.st.q1("SELECT COALESCE(SUM(credits),0) AS c FROM spend WHERE wf=? AND minute>=?",
                           (wid, day * MIN_PER_DAY))["c"]
        fair = max(12.0, self.credits / max(len(self.specs), 1))
        if self.credits < cost or spent + cost > fair:
            with self.st.tx():
                self.st.alert(self.now, wid, "page", "credits_low",
                              f"credit spend blocked: remaining {self.credits}, spent today {spent}, cap {fair:.0f}")
            return False
        return True

    def _spend(self, wid, kind, path, body, cost):
        with self.st.tx():
            cur = self.st.x("INSERT INTO spend(minute, wf, kind, request, credits, status) VALUES (?,?,?,?,?,?)",
                            (self.now, wid, kind, dumps(body), cost, "intent"))
            sid = cur.lastrowid
        try:
            r = self.sim.post(path, body)
        except ApiErr as e:
            with self.st.tx():
                self.st.x("UPDATE spend SET status=?, response=? WHERE id=?", ("error", str(e), sid))
                if e.status == 402:
                    self.st.alert(self.now, wid, "page", "out_of_credits", str(e))
            return None
        self.credits = r.get("credits_remaining", self.credits - cost)
        with self.st.tx():
            self.st.x("UPDATE spend SET status=?, response=? WHERE id=?", ("done", dumps(r), sid))
            self.st.decide(self.now, wid, f"spend_{kind}", f"{kind}: {cost} credits", {"request": body, "response": r})
        return r

    # ================================================================== experiments (C6/C7)
    def decay_check(self, wid, w):
        if self.now < 0:
            return
        today = self.now // MIN_PER_DAY
        exp = self.st.get_state("exp_state", wid)
        if not exp or not any(exp[d]["rules"] for d in X.DIMS):
            return
        events = X.look(w, exp, today, self.now, decay_only=True)
        if not events:
            with self.st.tx():
                self.st.put_state("exp_state", wid, exp)
            return
        with self.st.tx():
            self.st.put_state("exp_state", wid, exp)
            for e in events:
                self.st.decide(self.now, wid, e["kind"], e["summary"], e)
        if self.gov.can_request(wid, self.now, urgent=True) and not self.gov.pending(wid, ("rollback",)):
            new, notes = self.build_config(wid, w, exp, keep_did=True)
            if planner.canon(new) != planner.canon(self.cfg[wid]["config"]):
                self.request(wid, w, "rollback", new, urgent=True,
                             summary="Roll back: " + "; ".join(f"{e['dim']}={e['arm']} in {e['seg']} back to "
                                                              f"{e['control']}" for e in events),
                             rationale="A promoted variant stopped beating the control holdout:\n" +
                                       "\n".join(e["summary"] for e in events) +
                                       "\nRolling back to the previously approved control. " + " ".join(notes),
                             evidence={"events": events})

    def build_config(self, wid, w, exp, keep_did: bool):
        ws = self.st.get_state("wf_state", wid)
        cur = self.cfg[wid]["config"]
        caller = None
        if not keep_did:
            pol = self.specs[wid]["governance"]["auto_apply"]
            if not (pol.get("enabled") and "caller_id" in pol.get("allowed_dims", [])):
                hv = ws.get("did_health") or {}
                health = {d: {"status": s["status"], "health": hv.get(d, 1.0)}
                          for d, s in ws.get("did", {}).get("status", {}).items()}
                if health:
                    c0 = cur["allocations"]["default"]["caller_id"]
                    caller = callerid.target(health, c0, max_probes=2)   # approvals are scarce: probe 2 at once
                    if not callerid.worth_it(c0, caller, health):
                        caller = None
        sched = ws.get("schedule")
        return cohorts.build(cur, exp, sched, list(X.DIMS), caller)

    def plan(self, wid, w):
        """The daily batched proposal: experiments + cohorts + call times (+ caller IDs if not auto)."""
        if self.now < 0:
            return
        today = self.now // MIN_PER_DAY
        ws = self.st.get_state("wf_state", wid)
        if ws.get("plan_day") == today or not self.gov.approver_open(wid, self.now, routine=True):
            return
        if self.now < ws.get("supervisor_cooldown_until", -1):
            return
        if self.gov.pending(wid) or not self.gov.can_request(wid, self.now, urgent=False):
            return
        inc = ws.get("incident")
        if inc and inc["status"] != "closed":
            return
        exp = self.st.get_state("exp_state", wid)
        events = X.look(w, exp, today, self.now)
        new, notes = self.build_config(wid, w, exp, keep_did=False)
        cur = self.cfg[wid]["config"]
        with self.st.tx():
            self.st.put_state("exp_state", wid, exp)
            for e in events:
                self.st.decide(self.now, wid, e["kind"], e["summary"], e)
            ws["plan_day"] = today
            self.st.put_state("wf_state", wid, ws)
        material = self.material(cur, new, events)
        if not material:
            with self.st.tx():
                self.st.decide(self.now, wid, "plan_no_change",
                               "daily look: nothing material to change (no new winner/stop, allocation moves < 10%)",
                               {"notes": notes, "events": events})
            return
        summary, rationale = self.describe(wid, w, exp, events, cur, new, notes)
        self.request(wid, w, "plan", new, urgent=False, summary=summary, rationale=rationale,
                     evidence={"events": events})

    @staticmethod
    def material(cur: dict, new: dict, events: list) -> bool:
        """Is this worth an approver's attention? New statistical facts always are; otherwise
        only structural changes or allocation moves of >= 10% of some cohort's traffic."""
        if planner.canon(new) == planner.canon(cur):
            return False
        if any(e["kind"] in ("winner", "harm_stop", "decay_rollback", "futility_stop") for e in events):
            return True
        dims = planner.changed_dims(cur, new)
        if {"cohorts", "schedule", "prompt_version"} & set(dims):
            return True
        c0, c1 = cohorts_dist(cur, "default", "caller_id"), cohorts_dist(new, "default", "caller_id")
        if set(c0) != set(c1):
            return True
        names = {"default"} | {c["id"] for c in new.get("cohorts") or []}
        for k in names:
            for d in ("welcome_message", "voice", "caller_id"):
                a = cohorts_dist(cur, k, d)
                b = cohorts_dist(new, k, d)
                if callerid.tv(a, b) >= 0.10:
                    return True
        return False

    def describe(self, wid, w, exp, events, cur, new, notes):
        parts, why = [], []
        for e in events:
            if e["kind"] == "winner":
                parts.append(f"use {e['arm']} ({e['dim'].replace('_', ' ')}) for {e['seg']}")
            elif e["kind"] in ("harm_stop", "decay_rollback"):
                parts.append(f"stop {e['arm']} for {e['seg']}")
            why.append("- " + e["summary"])
        dims = planner.changed_dims(cur, new)
        exploring = {d: [a for a in exp[d]["active"]] for d in X.DIMS if exp[d]["active"]}
        if exploring and ("welcome_message" in dims or "voice" in dims) and not parts:
            parts.append("test " + ", ".join(f"{d.replace('_', ' ')} {'/'.join(a)} on 10% each" for d, a in
                                             exploring.items()))
        if "schedule" in dims:
            parts.append("call each age/lead group in its better hours")
        if "caller_id" in dims:
            parts.append("rebalance caller IDs")
        if not parts:
            parts.append("update cohorts")
        summary = "; ".join(parts)[:240]
        ws = self.st.get_state("wf_state", wid)
        sch = ws.get("schedule")
        rat = [f"Workflow {wid}: primary KPI {self.specs[wid]['objective']['primary']}, guardrail "
               f"{json.dumps(self.specs[wid]['objective'].get('guardrail'))}."]
        if why:
            rat.append("Evidence since last look:\n" + "\n".join(why))
        for d in X.DIMS:
            s = exp[d]
            rat.append(f"{d}: control {s['control']}; testing {s['active'] or 'nothing'}; winners "
                       f"{[(r['arm'], seg_key(r['seg'])) for r in s['rules']] or 'none yet'}; stopped "
                       f"{sorted(set(s['dropped'] + [h['arm'] + '@' + seg_key(h['seg']) for h in s['harm']]))}")
        if "schedule" in dims and sch:
            rat.append(f"Call times: in history, {sch['feature']} groups pick up at different hours (spread "
                       f"{sch['spread']}); weights shift *who* is dialled each hour, never how many, and every hour "
                       f"keeps weight >= 0.25. Pickup estimate is an upper bound and is checked after apply.")
        rat.append("Exploration: each untested option gets 10% of calls; a promoted winner keeps a 20% control "
                   "holdout so decay is visible. Winners need Holm-corrected significance and >= 300 answered calls "
                   "per side. Welcome/voice cannot change pickup (they play after the call is answered).")
        if notes:
            rat.append("Notes: " + " ".join(notes))
        return summary, "\n".join(rat)

    # ================================================================== request helper
    def request(self, wid, w, kind, new, urgent, summary, rationale, evidence=None) -> str | None:
        cfg = self.cfg[wid]
        cur = cfg["config"]
        dims = planner.changed_dims(cur, new)
        if not dims:
            return None
        today = self.now // MIN_PER_DAY
        exp = self.st.get_state("exp_state", wid)
        ws = self.st.get_state("wf_state", wid)
        health = ws.get("did_health")
        sch = ws.get("schedule")
        dr, e_lift, p_lift = planner.dry_run(w, exp, cur, new, today, health,
                                             sch["estimate"] if sch and "schedule" in dims else None)
        imp = 1.0 if kind in ("incident", "did_urgent") else planner.impact(
            self.specs[wid]["objective"]["primary"], e_lift, p_lift)
        if kind == "rollback":
            imp = max(imp, 0.6)
        body = {"proposal_id": self.gov.next_id(wid, kind, self.now), "workflow_id": wid,
                "base_version": cfg["version"], "proposed_config": new, "change_summary": summary,
                "diff": planner.diff(cur, new) or {"changed_dims": dims},
                "affected_leads": planner.affected_leads(w, cur, new, today, dims), "dry_run": dr,
                "impact_score": imp, "rationale": rationale, "evidence": evidence or {}}
        if "prompt_version" not in dims:
            body["evidence"] = {k: v for k, v in body["evidence"].items() if k != "validation_run_id"}
        r = self.gov.request(self.now, wid, kind, json.loads(dumps(body)), {"dims": dims, "urgent": urgent})
        return body["proposal_id"] if r.get("approval_id") or r.get("status") == "pending" else None

    # ================================================================== metrics (C14)
    def metrics(self, state, nev, t_ing, t_dec):
        pending = self.st.q1("SELECT COUNT(*) AS n FROM proposals WHERE status IN ('pending','approved')")["n"]
        m = {"wall": time.time(), "events": nev, "ingest_s": round(t_ing, 3), "decide_s": round(t_dec, 3),
             "events_per_s": round(nev / t_ing, 1) if t_ing > 0 and nev else 0,
             "rss_mb": round(rss_mb(), 1), "credits": state["credits_remaining"], "pending_approvals": pending,
             "last_event_id": state["last_event_id"], "events_total": self.ing.events_total}
        with self.st.tx():
            self.st.x("INSERT OR REPLACE INTO metrics(minute, data) VALUES (?,?)", (self.now, dumps(m)))
            if self.ing.events_total < state["last_event_id"]:
                self.st.alert(self.now, None, "page", "ingest_behind",
                              f"ingested {self.ing.events_total} < produced {state['last_event_id']}")
            else:
                self.st.resolve(self.now, None, "ingest_behind")
        if self.now % MIN_PER_DAY == 0:
            log.info("D%d: %s", self.now // MIN_PER_DAY, m)
