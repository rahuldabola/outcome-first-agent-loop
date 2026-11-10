"""Controller tests: the pieces where a bug would be silent and expensive.

Run: python -m pytest -q tests
(The tests may import `sim` to validate our configs against the simulator's own rules; the
controller itself never does.)
"""
import copy
import json

import numpy as np
import pytest

from controller import callerid, cohorts, experiments as X, stats
from controller.features import (N_CODES, cohort_index, encode, exact_changed_dims, mask, parse_seg, quantize,
                                 seg_key)
from controller.governance import Governance
from controller.ingest import Ingestor, WfData
from controller.store import Store

SPEC = {
    "workflow_id": "wf1", "tenant_id": "t1", "dialling_window": [9, 21], "dials_per_hour": 300, "cadence": [2, 3, 3],
    "daily_batch": {"size": 1000, "at_hour": 8}, "mid_flight": "allowed",
    "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
    "options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2", "V3", "V4"],
                "caller_id": ["D1", "D2", "D3"], "prompt_version": ["P1", "P3"]},
    "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 0.2},
                   "approver_hours": [10, 19], "max_requests_per_24h": 3, "proposal_ttl_hours": 24,
                   "apply_window_hours": 2, "min_validation_pass_rate": 0.97},
}
BASE_CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
                                                       "caller_id": {"D1": 0.5, "D2": 0.5}}},
            "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


# ------------------------------------------------------------------ features / shared cohort object
def test_encode_mask_roundtrip():
    c = encode({"lang": "ta", "city_tier": "2", "age_band": "60+", "value_band": "high"})
    assert 0 <= c < N_CODES
    assert mask({"lang": ["ta"]})[c] and mask({"age_band": ["46-60", "60+"]})[c]
    assert not mask({"lang": ["hi"]})[c]
    assert mask({}).all() and mask({"lang": ["ta"]}).sum() == N_CODES // 7
    assert encode({"lang": "xx", "city_tier": "1", "age_band": "60+", "value_band": "low"}) == -1
    k = seg_key({"lang": ["ta"], "age_band": ["60+", "46-60"]})
    assert k == "lang=ta;age_band=46-60|60+" and parse_seg(k) == {"lang": ["ta"], "age_band": ["46-60", "60+"]}


def test_quantize_is_exact_and_renormalisation_is_noop():
    for d in ({"D1": 0.1, "D2": 0.2, "D3": 0.7}, {"a": 1, "b": 1, "c": 1}, {"x": 0.333, "y": 0.667}):
        q = quantize(d)
        assert sum(q.values()) == 1.0
        assert {k: v / sum(q.values()) for k, v in q.items()} == q   # what the simulator does


def test_cells_partition_every_lead_exactly_once_and_respect_segments():
    exp = X.init_state(WfData(SPEC), BASE_CFG)
    exp["voice"]["rules"] = [{"seg": {"lang": ["ta"]}, "arm": "V2", "lift": 0.1},
                             {"seg": {"age_band": ["46-60", "60+"]}, "arm": "V3", "lift": 0.08}]
    exp["voice"]["harm"] = [{"seg": {"age_band": ["18-30"]}, "arm": "V3"}]
    exp["welcome_message"]["rules"] = [{"seg": {}, "arm": "W2", "lift": 0.05}]
    sched = {"feature": "age_band", "weights": {v: {str(h): 1.0 for h in range(9, 21)}
                                                for v in ["18-30", "31-45", "46-60", "60+"]}}
    cfg, notes = cohorts.build(BASE_CFG, exp, sched, list(X.DIMS), None)
    assert len(cfg["cohorts"]) <= 20
    names, idx = cohort_index(cfg)
    assert (idx > 0).all(), "cells must cover every lead (nothing falls to default)"
    seen = np.zeros(N_CODES, int)
    for c in cfg["cohorts"]:
        seen += mask(c["where"])
    assert (seen == 1).all(), "cells must not overlap"
    # every segment used by a rule is an exact union of cells
    for r in exp["voice"]["rules"]:
        m = mask(r["seg"])
        for c in cfg["cohorts"]:
            cm = mask(c["where"])
            assert (cm & m).sum() in (0, cm.sum())
    # a ta 60+ lead: both V2 (ta) and V3 (46+) rules apply -> larger lift wins
    code = encode({"lang": "ta", "city_tier": "1", "age_band": "60+", "value_band": "low"})
    cid = names[idx[code]]
    vd = cfg["allocations"][cid]["voice"]
    assert max(vd, key=vd.get) == "V2" and vd["V1"] == pytest.approx(0.2, abs=0.01)   # 20% holdout on control
    # an 18-30 non-ta lead: V3 is harmful there -> never assigned
    code = encode({"lang": "hi", "city_tier": "1", "age_band": "18-30", "value_band": "low"})
    vd = cfg["allocations"].get(names[idx[code]], {}).get("voice") or cfg["allocations"]["default"]["voice"]
    assert "V3" not in vd


def test_built_config_passes_the_simulators_validator():
    from sim.world import normalise_config
    spec = {**SPEC, "default_config": BASE_CFG}
    exp = X.init_state(WfData(SPEC), BASE_CFG)
    exp["voice"]["rules"] = [{"seg": {"lang": ["ta"]}, "arm": "V2", "lift": 0.1}]
    sched = {"feature": "age_band", "weights": {v: {str(h): 1.2 for h in range(9, 21)}
                                                for v in ["18-30", "31-45", "46-60", "60+"]}}
    cfg, _ = cohorts.build(BASE_CFG, exp, sched, list(X.DIMS), {"D1": 0.4, "D2": 0.4, "D3": 0.2})
    normalise_config(spec, cfg)   # raises on any violation


def test_foreign_cohorts_are_kept_first_and_untouched():
    cur = copy.deepcopy(BASE_CFG)
    cur["cohorts"] = [{"id": "sup_seniors", "where": {"age_band": ["60+"]}}]
    cur["allocations"]["sup_seniors"] = {"voice": {"V4": 1.0}}
    exp = X.init_state(WfData(SPEC), BASE_CFG)
    exp["voice"]["rules"] = [{"seg": {"lang": ["ta"]}, "arm": "V2", "lift": 0.1}]
    cfg, _ = cohorts.build(cur, exp, None, list(X.DIMS), None)
    assert cfg["cohorts"][0] == cur["cohorts"][0]
    assert cfg["allocations"]["sup_seniors"] == {"voice": {"V4": 1.0}}


# ------------------------------------------------------------------ stats
def test_mh_matches_simple_risk_ratio_on_one_stratum():
    c = stats.mh([60], [100], [50], [100])
    assert c.rr == pytest.approx(1.2)
    assert c.n_arm == 100 and c.rate_ctl == 0.5


def test_mh_removes_simpsons_paradox():
    # arm is 10% better inside each stratum, but sits mostly in the low-rate stratum
    a_s, a_n = np.array([22, 330]), np.array([40, 1000])
    c_s, c_n = np.array([200, 30]), np.array([400, 100])
    raw = (a_s.sum() / a_n.sum()) / (c_s.sum() / c_n.sum())
    assert raw < 1
    assert stats.mh(a_s, a_n, c_s, c_n).rr == pytest.approx(1.1, abs=0.01)


def test_holm():
    assert stats.holm([0.001, 0.02, 0.04], 0.05) == [True, True, True]
    assert stats.holm([0.001, 0.03, 0.04], 0.05) == [True, False, False]


# ------------------------------------------------------------------ experiments: winner / harm / decay
def _cube_world(rates_by_day: dict, n_per_arm=600):
    """WfData with eng cubes where voice arm rates are given per day (welcome fixed W1)."""
    w = WfData(SPEC)
    rng = np.random.default_rng(0)
    for day, rates in rates_by_day.items():
        E = w.cube("eng", day)
        for arm, (rate, n) in rates.items():
            vi = w.oi["voice"][arm]
            codes = rng.integers(0, N_CODES, n)
            eng = rng.random(n) < rate
            np.add.at(E, (codes, 0, vi, 0, 0), 1)
            np.add.at(E, (codes, 0, vi, 0, 1), eng.astype(np.int32))
    return w


def test_clear_winner_promoted_and_harmful_arm_stopped():
    days = {d: {"V1": (0.50, 1500), "V2": (0.62, 400), "V3": (0.50, 400), "V4": (0.38, 400)} for d in range(0, 3)}
    w = _cube_world(days)
    st = X.init_state(w, BASE_CFG)
    ev = X.look(w, st, 2, 2 * 1440 + 600)
    kinds = {(e["kind"], e.get("arm"), e.get("seg")) for e in ev}
    assert ("winner", "V2", "ALL") in kinds
    assert ("harm_stop", "V4", "ALL") in kinds and "V4" in st["voice"]["dropped"]
    assert not any(e["kind"] == "winner" and e["arm"] == "V3" for e in ev)


def test_decayed_winner_rolled_back():
    days = {d: {"V1": (0.5, 400), "V2": (0.62, 1600)} for d in range(0, 3)}
    days.update({d: {"V1": (0.5, 400), "V2": (0.40, 1600)} for d in range(3, 5)})
    w = _cube_world(days)
    st = X.init_state(w, BASE_CFG)
    X.look(w, st, 2, 2 * 1440 + 600)
    assert st["voice"]["rules"]
    ev = X.look(w, st, 4, 4 * 1440 + 600, decay_only=True)
    assert any(e["kind"] == "decay_rollback" for e in ev) and not st["voice"]["rules"]


def test_aa_no_winner_on_identical_arms():
    days = {d: {"V1": (0.55, 1500), "V2": (0.55, 500), "V3": (0.55, 500), "V4": (0.55, 500)} for d in range(0, 4)}
    w = _cube_world(days)
    st = X.init_state(w, BASE_CFG)
    assert not [e for e in X.look(w, st, 3, 3 * 1440 + 600) if e["kind"] == "winner"]


def test_supervisor_choice_freezes_exploration():
    st = X.init_state(WfData(SPEC), BASE_CFG)
    ev = X.set_control(st, "voice", "V2", SPEC["options"]["voice"], 100)
    assert ev and st["voice"]["control"] == "V2" and st["voice"]["active"] == []


# ------------------------------------------------------------------ caller IDs
def test_step_toward_respects_max_shift_and_touches_only_caller_id():
    cur = {"D1": 0.5, "D2": 0.5}
    tgt = {"D1": 0.0, "D2": 0.5, "D3": 0.5}
    step = callerid.step_toward(cur, tgt, 0.2)
    assert callerid.tv(cur, step) <= 0.2 + 1e-9
    old = copy.deepcopy(BASE_CFG)
    old["allocations"]["default"]["welcome_message"] = quantize({"W1": 0.8, "W2": 0.1, "W3": 0.1})
    new = copy.deepcopy(old)
    new["allocations"]["default"]["caller_id"] = step
    assert exact_changed_dims(old, new) == ["caller_id"]


def test_flagged_number_is_never_targeted():
    health = {"D1": {"status": "healthy", "health": 1.0}, "D2": {"status": "flagged", "health": 0.4},
              "D3": {"status": "healthy", "health": 1.0}}
    t = callerid.target(health, {"D1": 0.5, "D2": 0.5})
    assert "D2" not in t and sum(t.values()) == 1.0


# ------------------------------------------------------------------ ingestion: exactly-once + restore
def _chunk(events):
    return "\n".join(json.dumps(e) for e in events).encode()


def test_ingest_commit_and_restore_reproduce_state(tmp_path):
    st = Store(tmp_path / "s.db")
    spec = {**SPEC}
    ing = Ingestor(st, {"wf1": spec})
    with st.tx():
        ing.set_origin(0, "2027-01-04T00:00:00+05:30")
    lead = {"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": "wf1", "campaign_id": "wf1:c1",
            "lead_id": "wf1:l1", "person_id": "wf1:p1",
            "features": {"lang": "hi", "city_tier": "1", "age_band": "31-45", "value_band": "low"}}
    call = {"type": "call", "ts": "2027-01-04T10:05:00+05:30", "workflow_id": "wf1", "campaign_id": "wf1:c1",
            "lead_id": "wf1:l1", "variant": {"welcome_message": "W2", "voice": "V3", "caller_id": "D2",
                                             "prompt_version": "P1"},
            "connected": True, "engaged": True, "qa": {"hallucination_flag": False}}
    ing.ingest_chunk(0, _chunk([lead]), lambda e: None, lambda e: None)
    ing.ingest_chunk(1, _chunk([call]), lambda e: None, lambda e: None)
    assert st.get("last_chunk") == 1 and st.get("events_total") == 2
    ing2 = Ingestor(st, {"wf1": spec})
    ing2.restore(0)
    for kind in ("eng", "pick", "mon"):
        assert np.array_equal(getattr(ing.wf["wf1"], kind)[0], getattr(ing2.wf["wf1"], kind)[0])
    assert ing2.wf["wf1"].eng[0][..., 1].sum() == 1
    assert ing2.wf["wf1"].active_by_code(0).sum() == 0     # the lead concluded


def test_failed_transaction_does_not_advance_cursor(tmp_path):
    st = Store(tmp_path / "s.db")
    ing = Ingestor(st, {"wf1": SPEC})
    with st.tx():
        ing.set_origin(0, "2027-01-04T00:00:00+05:30")
    ev = {"type": "config_changed", "ts": "2027-01-04T10:00:00+05:30", "workflow_id": "wf1", "version": 2,
          "actor": "supervisor", "changed_dims": ["voice"]}

    def boom(_):
        raise RuntimeError("crash inside the transaction")
    with pytest.raises(RuntimeError):
        ing.ingest_chunk(5, _chunk([ev]), boom, lambda e: None)
    assert st.get("last_chunk") is None


# ------------------------------------------------------------------ governance: crash reconciliation
class FakeSim:
    def __init__(self):
        self.approvals, self.history, self.posts = {}, [], []

    def post(self, path, body):
        self.posts.append((path, body))
        if path == "/approvals":
            aid = f"apr_{len(self.approvals) + 1:05d}"
            self.approvals[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"], "status": "pending"}
            return {"approval_id": aid, "status": "pending"}
        if path.endswith("/apply"):
            a = self.approvals[body["approval_id"]]
            a["status"] = "applied"
            self.history.append({"version": 2, "approval_id": a["approval_id"], "proposal_id": a["proposal_id"]})
            return {"version": 2, "changed_dims": ["voice"]}
        raise AssertionError(path)

    def get(self, path, **params):
        if path == "/approvals":
            return list(self.approvals.values())
        if path.startswith("/approvals/"):
            return self.approvals[path.split("/")[-1]]
        if path.endswith("/config/history"):
            return self.history
        raise AssertionError(path)


def _body(pid):
    return {"proposal_id": pid, "workflow_id": "wf1", "base_version": 1, "proposed_config": {}, "change_summary": "s",
            "diff": {"x": 1}, "affected_leads": 1, "dry_run": {"x": 1}, "impact_score": 0.1, "rationale": "r"}


def test_reconcile_adopts_approval_created_before_crash(tmp_path):
    st, sim = Store(tmp_path / "s.db"), FakeSim()
    gov = Governance(st, sim, {"wf1": SPEC}, {})
    # crash between POST /approvals and recording its id: row stays 'intent', sim has the approval
    with st.tx():
        st.x("INSERT INTO proposals(proposal_id, wf, kind, channel, status, base_version, created_minute, body) "
             "VALUES ('p1','wf1','plan','approval','intent',1,600,?)", (json.dumps({"request": _body("p1")}),))
    sim.approvals["apr_00001"] = {"approval_id": "apr_00001", "proposal_id": "p1", "status": "approved"}
    gov.reconcile(700)
    row = st.q1("SELECT * FROM proposals WHERE proposal_id='p1'")
    assert row["approval_id"] == "apr_00001" and row["status"] == "approved"
    assert not sim.posts, "must not resubmit an approval the simulator already has"


def test_reconcile_never_applies_twice(tmp_path):
    st, sim = Store(tmp_path / "s.db"), FakeSim()
    gov = Governance(st, sim, {"wf1": SPEC}, {})
    gov.request(600, "wf1", "plan", _body("p2"), {})
    aid = st.q1("SELECT approval_id FROM proposals WHERE proposal_id='p2'")["approval_id"]
    sim.approvals[aid]["status"] = "approved"
    with st.tx():
        st.x("UPDATE proposals SET status='approved' WHERE proposal_id='p2'")
    gov.apply_approved(700)
    # crash after the apply landed but before we recorded it
    with st.tx():
        st.x("UPDATE proposals SET status='applying' WHERE proposal_id='p2'")
    n_posts = len(sim.posts)
    gov.reconcile(760)
    assert st.q1("SELECT status FROM proposals WHERE proposal_id='p2'")["status"] == "applied"
    assert len(sim.posts) == n_posts, "reconcile must not call apply again"


def test_request_budget_keeps_a_reserve_for_incidents(tmp_path):
    st, sim = Store(tmp_path / "s.db"), FakeSim()
    gov = Governance(st, sim, {"wf1": SPEC}, {})
    gov.request(600, "wf1", "plan", _body("a"), {})
    gov.request(610, "wf1", "plan", _body("b"), {})
    assert not gov.can_request("wf1", 620, urgent=False)
    assert gov.can_request("wf1", 620, urgent=True)
    assert gov.can_request("wf1", 600 + 1441, urgent=False)


def test_tenant_prefs_only_tighten(tmp_path):
    st, sim = Store(tmp_path / "s.db"), FakeSim()
    gov = Governance(st, sim, {"wf1": SPEC}, {"t1": {"max_requests_per_day": 2, "earliest_request_hour": 11}})
    assert gov.limit("wf1") == 2
    assert not gov.approver_open("wf1", 10 * 60 + 30, routine=True)
    assert gov.approver_open("wf1", 10 * 60 + 30, routine=False)
