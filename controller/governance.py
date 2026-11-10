"""Governance client (C9): every config change goes through here, and only here.

Proposal lifecycle (our `proposals.status`):

    approval channel:   intent -> pending -> approved -> applying -> applied
                                           -> rejected | expired           (terminal; re-plan with fresh data)
                                 approved -> lapsed                         (apply window missed)
                                 applying -> stale                          (config moved under us; re-plan)
                        intent -> stale | invalid                           (refused at submit)
    auto channel:       intent -> applied | refused | stale

Crash safety: a row is written as `intent` (and committed) BEFORE any external call, using a
deterministic proposal_id. On restart, `reconcile()` asks the simulator what really happened to
each non-terminal row (GET /approvals, GET /config/history) and continues from there. The
simulator's proposal_id uniqueness makes resubmission safe: we never create a second approval
for the same intent and never apply one twice (apply is only attempted from status `approved`,
and a successful apply flips the approval to `applied` on the server too).
"""
from __future__ import annotations

import json
import logging

from .client import ApiErr
from .store import dumps

log = logging.getLogger("ctl.gov")
TERMINAL = {"applied", "rejected", "expired", "lapsed", "stale", "invalid", "refused", "failed"}


class Governance:
    def __init__(self, store, sim, specs: dict, prefs: dict):
        self.st, self.sim, self.specs, self.prefs = store, sim, specs, prefs

    # ------------------------------------------------------------------ budget & timing
    def limit(self, wf: str) -> int:
        spec = self.specs[wf]
        lim = spec["governance"]["max_requests_per_24h"]
        p = self.prefs.get(spec["tenant_id"], {}).get("max_requests_per_day")
        return min(lim, p) if p else lim

    def used_24h(self, wf: str, now: int) -> int:
        r = self.st.q1("SELECT COUNT(*) AS n FROM proposals WHERE wf=? AND channel='approval' AND created_minute>? "
                       "AND status NOT IN ('invalid','stale_submit')", (wf, now - 1440))
        return r["n"]

    def can_request(self, wf: str, now: int, urgent: bool) -> bool:
        lim = self.limit(wf)
        reserve = 1 if lim >= 2 else 0
        used = self.used_24h(wf, now)
        return used + 1 <= (lim if urgent else lim - reserve)

    def approver_open(self, wf: str, minute: int, routine: bool) -> bool:
        spec = self.specs[wf]
        lo, hi = spec["governance"]["approver_hours"]
        h = (minute % 1440) / 60
        if routine:
            lo = max(lo, self.prefs.get(spec["tenant_id"], {}).get("earliest_request_hour", lo))
            # leave room for the slowest approver to answer inside today's hours
            return lo <= h < hi - 1
        return lo <= h < hi

    def pending(self, wf: str, kinds: tuple[str, ...] | None = None) -> list[dict]:
        rows = self.st.q("SELECT * FROM proposals WHERE wf=? AND status IN ('intent','pending','approved','applying')",
                         (wf,))
        return [r for r in rows if kinds is None or r["kind"] in kinds]

    def next_id(self, wf: str, kind: str, minute: int) -> str:
        n = self.st.q1("SELECT COUNT(*) AS n FROM proposals")["n"] + 1
        return f"au-{wf}-{kind}-m{minute}-{n:05d}"

    # ------------------------------------------------------------------ submit
    def request(self, minute: int, wf: str, kind: str, body: dict, meta: dict) -> dict:
        pid = body["proposal_id"]
        with self.st.tx():
            self.st.x("INSERT INTO proposals(proposal_id, wf, kind, channel, status, base_version, created_minute, "
                      "body, updated_minute) VALUES (?,?,?,?,?,?,?,?,?)",
                      (pid, wf, kind, "approval", "intent", body["base_version"], minute,
                       dumps({"request": body, "meta": meta}), minute))
        return self._submit_approval(minute, pid, body)

    def _submit_approval(self, minute, pid, body):
        try:
            r = self.sim.post("/approvals", body)
        except ApiErr as e:
            if e.code == "duplicate_proposal_id":
                return self._reconcile_one(minute, self.st.q1("SELECT * FROM proposals WHERE proposal_id=?", (pid,)))
            status = "stale" if e.code == "stale_base_version" else "invalid"
            with self.st.tx():
                self._set(pid, minute, status=status if status != "stale" else "stale_submit",
                          reasons=[f"{e.status} {e.code}: {e.detail}"])
                self.st.decide(minute, body["workflow_id"], "proposal_refused",
                               f"submit refused ({e.code})", {"detail": e.detail}, pid)
                if status == "invalid":
                    self.st.alert(minute, body["workflow_id"], "ticket", "invalid_proposal",
                                  f"simulator refused proposal {pid}: {e.code} {e.detail}")
            return {"status": status}
        with self.st.tx():
            self._set(pid, minute, status="pending", approval_id=r["approval_id"])
            self.st.decide(minute, body["workflow_id"], "approval_requested", body["change_summary"],
                           {"approval_id": r["approval_id"], "impact_score": body["impact_score"],
                            "affected_leads": body["affected_leads"], "expires_at": r.get("expires_at")}, pid)
        return r

    def auto(self, minute: int, wf: str, kind: str, body: dict, meta: dict) -> dict:
        pid = body["proposal_id"]
        with self.st.tx():
            self.st.x("INSERT INTO proposals(proposal_id, wf, kind, channel, status, base_version, created_minute, "
                      "body, updated_minute) VALUES (?,?,?,?,?,?,?,?,?)",
                      (pid, wf, kind, "auto", "intent", body["base_version"], minute,
                       dumps({"request": body, "meta": meta}), minute))
        return self._submit_auto(minute, wf, pid, body)

    def _submit_auto(self, minute, wf, pid, body):
        try:
            r = self.sim.put(f"/workflows/{wf}/config", body)
        except ApiErr as e:
            if e.code == "duplicate_proposal_id":
                return self._reconcile_one(minute, self.st.q1("SELECT * FROM proposals WHERE proposal_id=?", (pid,)))
            with self.st.tx():
                self._set(pid, minute, status="stale" if e.code == "stale_base_version" else "refused",
                          reasons=[f"{e.status} {e.code}: {e.detail}"])
                self.st.decide(minute, wf, "auto_apply_refused", f"auto-apply refused ({e.code})",
                               {"detail": e.detail}, pid)
            return {"status": "refused"}
        with self.st.tx():
            self._set(pid, minute, status="applied", applied_version=r["version"], decided_minute=minute)
            self.st.decide(minute, wf, "auto_applied", body["rationale"].split("\n")[0],
                           {"version": r["version"], "changed_dims": r["changed_dims"]}, pid)
        return {"status": "applied", **r}

    # ------------------------------------------------------------------ decisions & apply
    def on_decided(self, ev: dict, minute: int):
        """Called inside the ingest transaction for each approval_decided event."""
        row = self.st.q1("SELECT * FROM proposals WHERE approval_id=?", (ev["approval_id"],))
        if not row or row["status"] not in ("pending", "intent"):
            return
        self._set(row["proposal_id"], minute, status=ev["status"], reasons=ev.get("reasons") or [],
                  decided_minute=minute)
        self.st.decide(minute, row["wf"], f"approval_{ev['status']}",
                       f"{ev['approval_id']} {ev['status']}" + (f": {'; '.join(ev.get('reasons') or [])}"
                                                               if ev.get("reasons") else ""),
                       {"approval_id": ev["approval_id"], "reasons": ev.get("reasons")}, row["proposal_id"])
        if ev["status"] == "rejected" and any("fatigue" in r for r in ev.get("reasons") or []):
            self.st.alert(minute, row["wf"], "ticket", "approver_fatigue",
                          "approver fatigue rejection: request budget accounting is off")

    def apply_approved(self, minute: int) -> list[dict]:
        out = []
        for row in self.st.q("SELECT * FROM proposals WHERE status IN ('approved','applying')"):
            out.append(self._apply(minute, row))
        return out

    def _apply(self, minute, row):
        pid, wf = row["proposal_id"], row["wf"]
        with self.st.tx():
            self._set(pid, minute, status="applying")
        try:
            r = self.sim.post(f"/workflows/{wf}/apply", {"approval_id": row["approval_id"]})
        except ApiErr as e:
            if e.code == "not_approved":
                return self._reconcile_one(minute, self.st.q1("SELECT * FROM proposals WHERE proposal_id=?", (pid,)))
            status = {"approval_lapsed": "lapsed", "stale_base_version": "stale"}.get(e.code, "failed")
            with self.st.tx():
                self._set(pid, minute, status=status, reasons=[f"{e.status} {e.code}: {e.detail}"])
                self.st.decide(minute, wf, f"apply_{status}",
                               f"apply of {row['approval_id']} failed: {e.code} -> re-plan with fresh data",
                               {"detail": e.detail}, pid)
                if status == "failed":
                    self.st.alert(minute, wf, "page", "apply_failed", f"apply {row['approval_id']}: {e}")
            return {"proposal_id": pid, "status": status, "kind": row["kind"]}
        with self.st.tx():
            self._set(pid, minute, status="applied", applied_version=r["version"])
            self.st.decide(minute, wf, "applied", f"applied {row['approval_id']} as v{r['version']}",
                           {"version": r["version"], "changed_dims": r["changed_dims"]}, pid)
        return {"proposal_id": pid, "status": "applied", "kind": row["kind"], "version": r["version"]}

    # ------------------------------------------------------------------ crash recovery
    def reconcile(self, minute: int):
        rows = self.st.q("SELECT * FROM proposals WHERE status IN ('intent','pending','approved','applying')")
        for row in rows:
            self._reconcile_one(minute, row)
        if rows:
            log.info("reconciled %d in-flight proposals", len(rows))

    def _reconcile_one(self, minute, row):
        pid, wf = row["proposal_id"], row["wf"]
        body = json.loads(row["body"])["request"]
        if row["channel"] == "auto":
            hist = self.sim.get(f"/workflows/{wf}/config/history")
            hit = next((h for h in hist if h.get("proposal_id") == pid), None)
            if hit:
                with self.st.tx():
                    self._set(pid, minute, status="applied", applied_version=hit["version"])
                    self.st.decide(minute, wf, "reconciled", f"auto-apply {pid} had landed as v{hit['version']}",
                                   {}, pid)
                return {"status": "applied"}
            return self._submit_auto(minute, wf, pid, body)
        if row["approval_id"] is None:
            hit = next((a for a in self.sim.get("/approvals", workflow_id=wf) if a["proposal_id"] == pid), None)
            if hit is None:
                return self._submit_approval(minute, pid, body)
            with self.st.tx():
                self._set(pid, minute, approval_id=hit["approval_id"], status="pending")
            row = self.st.q1("SELECT * FROM proposals WHERE proposal_id=?", (pid,))
        a = self.sim.get(f"/approvals/{row['approval_id']}")
        s = a["status"]
        with self.st.tx():
            if s == "applied":
                hist = self.sim.get(f"/workflows/{wf}/config/history")
                v = next((h["version"] for h in hist if h.get("approval_id") == row["approval_id"]), None)
                self._set(pid, minute, status="applied", applied_version=v)
                self.st.decide(minute, wf, "reconciled", f"{row['approval_id']} was already applied (v{v})", {}, pid)
            elif s != row["status"] and not (row["status"] == "applying" and s == "approved"):
                self._set(pid, minute, status=s, reasons=a.get("reasons"))
                self.st.decide(minute, wf, "reconciled", f"{row['approval_id']} is {s}", {"reasons": a.get("reasons")},
                               pid)
            elif row["status"] == "applying" and s == "approved":
                self._set(pid, minute, status="approved")
        return {"status": s}

    # ------------------------------------------------------------------ helpers
    def _set(self, pid, minute, **kw):
        if "reasons" in kw:
            kw["reasons"] = dumps(kw["reasons"])
        kw["updated_minute"] = minute
        cols = ", ".join(f"{k}=?" for k in kw)
        self.st.x(f"UPDATE proposals SET {cols} WHERE proposal_id=?", (*kw.values(), pid))
