# Outcome-First Agent Loop — Submission

**Name:** Rahul Dabola · **Date:** 2026-11-11 · **Hours actually spent (be honest):** _[fill in]_ ·
**Language / storage:** Python 3.11 (numpy, httpx) / one SQLite file (WAL), no new services

---

## 0. TL;DR
- `./run.sh <SIM_URL>` runs a single-process, deterministic controller. It ingests events exactly once into per-workflow count cubes keyed by lead features. It runs factorial welcome × voice experiments with stratified Mantel-Haenszel tests and Holm correction. It rotates caller IDs on health and fixes bad prompts with validation evidence. Every change goes through `/approvals` or a policy-permitted caller-ID auto-apply.
- **public_01:** conclusion rate **+13.3%** vs baseline (pickup +10.5%, engagement +1.3%). 18 changes applied: **0 harmful**, 8 beneficial, 10 neutral. 5 approval requests, no fatigue rejections.
- **public_02:** conclusion rate **+22.1%** (pickup +18.2%, engagement +1.5%). 21 changes: **0 harmful**. The bad supervisor prompt was fixed in **90 min** for 6 credits with no LLM call. The spam-flagged caller ID went to 0% share the same day. A decayed welcome message was out of rotation by onset+7d.
- **public_03 (load):** +20.8% conclusion rate. 69 changes, **0 harmful**. 24–52k events/s ingest, **36–67 s wall per simulated day** (budget: 600 s), < 560 MB RSS. A kill -9 / restart run is **bit-identical** to an uninterrupted one.
- **Deliberately not built:** per-person call times, prompt *optimisation* (prompts change only as safety fixes), a UI (the supervisor view is a CLI), cross-tenant prioritisation.

## 1. Assumptions
1. `pickup_rate_max_drop: 0.02` is read as a *relative* 2% drop. It is the stricter of the two readings.
2. Welcome message and voice play after the call is answered, so they cannot causally move pickup. The pickup guardrail therefore applies to caller-ID and schedule changes. Variant experiments are judged on engagement only.
3. The feature domain (lang / city_tier / age_band / value_band values) is part of the public contract, since cohort `where` clauses must use it.
4. Stakeholder preferences can only make the controller *stricter* than the signed policy, never looser. Example: Kestrel's "2 a day, not before 11:00" is honoured; Sales' "auto-apply everything" is not.
5. Caller ID and call timing are "operational" (Northwind memo, item 1). Welcome, voice and prompt are "customer-facing". Customer-facing changes **always** go through approval, even where the policy would allow auto-apply (ks_cancel allows welcome/voice). That includes rollbacks.
6. A supervisor's edit is a business decision. It becomes the new control for that dimension, and overriding it needs 10× stronger evidence.
7. The controller may wait for the simulator. In production, ingestion would be a stream consumer and decisions would run on a timer. The logic is the same.
8. One controller process owns one store (single writer).

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Ingestion at load scale | **Built** | Everything downstream depends on it | Streaming consumer instead of chunk polling |
| C2 | Lead/feature join, one cohort object | **Built** | Every lead → one of 252 feature codes. A segment is a predicate with a canonical id; config cohorts are the cells of the coarsest partition respecting every segment in use | — |
| C3 | Bootstrap from history | **Built** | Call-time weights, prompt flag baselines, incumbent controls, caller-ID baseline | — |
| C4 | Call-time optimisation | **Partial** | Per-cohort hour weights fitted from history, on the feature with the most timing heterogeneity. Per-person cut (§3, Product) | Online re-fit; switchback test to prove the lift causally |
| C5 | Caller-ID health | **Built** | Biggest pickup lever, and mostly auto-appliable | Carrier/STIR feedback as a second signal |
| C6 | Welcome × voice experiments | **Built** | Core of engagement | Adaptive allocation once volume allows |
| C7 | Promotion, decay, rollback | **Built** | Permanent 20% control holdout makes decay visible | — |
| C8 | Hallucination incident response | **Built** | Regulated clients; MTTR is graded | — |
| C9 | Governance client | **Built** | Intent-logged lifecycle, batching, approver hours, incident reserve, stale/expired → re-plan | Explicit `expires_in_hours` tuning per approver latency |
| C10 | Impact / affected / dry run | **Partial** | Honest (shrunk lifts, unproven arms count as 0) but model-based replay, not counterfactual simulation | Replay against held-out history with off-policy estimators |
| C11 | Supervisor view | **Partial** | `python -m controller view`: one screen per workflow in markdown | Small web page over the same queries |
| C12 | Audit store | **Built** | `why`, `log`; every decision has its evidence | — |
| C13 | Crash recovery, demonstrated | **Built** | `scripts/crash_test.py`, `reports/crash_test.md` | — |
| C14 | Loop observability | **Partial** | Metrics per tick, `health`, alert table with page/ticket/info | Push to Prometheus / pager |
| C15 | Fair credits & attention | **Partial** | Per-workflow daily credit fair share; per-workflow request budget with a reserved incident slot | Cross-tenant priority queue for approver attention |
| — | Prompt optimisation | **Cut** | One prompt per workflow means no concurrent A/B; needs a switchback design and approvals for each switch | Day-level switchback with DiD analysis |

**Principle:** first, never make things worse (governance, harm stops, determinism, crash safety). Second, take the levers with the best evidence-per-approval: caller IDs (auto-appliable, randomised, large effect), then incidents, then experiments. Anything I could not make statistically honest at dev-scale volume (per-person timing, prompt optimisation) I cut rather than faked.

## 3. Stakeholders

### 01 — Sales (Nikhil)
- **Accepted:** speed where it's free. One batched request per workflow per day; incident fixes in ~90 min; per-client lift numbers with confidence intervals (`view`).
- **Declined:** a single blended score (README and Product forbid blending pickup and engagement). "Winning by lunch, live by evening": a lunch-time lead at dev volume is usually noise. Our A/A runs show how often noise looks like a winner. Rolling the "persuasive" prompt out everywhere: winners stay per-workflow (Product), and every prompt change needs validation evidence.
- **Wrong against policy:** "Kestrel turned on auto-apply for everything". ks_cancel allows only schedule/caller_id/welcome/voice, with `max_shift` 0.2, and never prompt or cohorts. "Northwind is fine with auto-apply on KYC". nw_kyc has `auto_apply.enabled: false`. A QBR remark is not a signed policy.
- **Reply:** "Nikhil — you'll get a real number per client: pickup and engagement lift vs. the no-change baseline, with the range, on one screen. Across our test scenarios that is +13% to +22% more leads concluded. I can't blend them into one score (Product rule), push winners by evening, or auto-apply on Kestrel/Northwind beyond what their signed policies allow. Kestrel caps auto-apply to small shifts and Northwind KYC has it off. The persuasive script needs validation and approval per client. 'Improved itself overnight, safely, with the client's sign-off' is the stronger summit story."

### 02 — Northwind Compliance (Meera)
- **Accepted:** no customer-facing change without a named approval. We never auto-apply welcome, voice or prompt, even where policy would allow it. Script changes carry `evidence.validation_run_id`. "Slow and right": harm stops, a permanent holdout, Holm-corrected winners, and supervisors' choices are protected. Requests go out only during your approver hours (10–19, KYC 10–18), so routine requests are never queued overnight.
- **Partial:** the auditor question "why did customer X hear Y on day D". Each call event carries `config_version` and variant. `python -m controller why --workflow W --at T` turns that version into the change, the evidence, the approver and what happened next. The per-call record itself lives in the dialler's call log; we don't duplicate it.
- **Wrong:** "caller-ID rotation is covered by the policy we signed". True for nw_renewal; **not** for nw_kyc (auto-apply disabled). Rotations there go through your approvers.
- **Reply:** "Meera — agreed on all five. Nothing a customer hears changes without your named approval, including rollbacks; script changes always carry a validation run. One correction: caller-ID rotation is pre-authorised only on Renewal; on KYC your policy has auto-apply off, so those come to your approvers too. For auditors, we can answer 'why was this variant live for this customer at this time' from our decision log in minutes, with the approver and timestamp. Operational changes appear in a weekly summary from the same log."

### 03 — Platform/SRE (Kabir)
- **Accepted, all of it:** kill/restart test (`scripts/crash_test.py`): 4 deterministic hard exits including mid-incident, plus 4 random SIGKILLs. Results: exactly-once ingestion, no double apply, no lost approval, nothing stuck, and an outcome **identical** to an uninterrupted run. Idempotency: an intent row is committed before every external call, with deterministic proposal ids and reconcile-on-boot. Paging is limited to actionable codes (RUNBOOK.md). One day of public_03 takes 36–67 s wall, simulator included. Loop health is visible without logs via `python -m controller health`.
- **On infra:** no new server. State is one SQLite file (an embedded library, nothing to operate at 2 a.m.); the schema is plain SQL and ports to your Postgres unchanged.
- **Reply:** "Kabir — kill test is in the repo (8 kills incl. random SIGKILLs; run completes identically, zero double-applies). No new stateful service: one SQLite file per controller, schema portable to our Postgres. One public_03 day takes ~1 minute wall including the simulator. At 10×, the first thing to break is single-process ingest (~40k events/s); the fix is sharding by workflow, which the design already allows (no cross-workflow state). Pages only for incidents, flagged numbers, blocked fixes, credit exhaustion and ingest lag; runbook included."

### 04 — Kestrel Ops supervisor (Anita)
- **Accepted:** we never silently undo your edits. After an edit, routine proposals pause 24 h, your choice becomes the control, exploration on that dimension stops, and overriding it needs 10× the evidence. Max 2 requests/day, never before 11:00 (`controller/prefs.json`); one of the two is held back for incidents, so the routine plan is one batched request. Customer-facing changes always come to you first. One screen: `view`. On "the verbatim script nobody stays on": validation measures only safety, so after any prompt change we also watch engagement vs the previous prompt and propose a revert if it drops.
- **Pushed back:** if a script you ship fails the safety bar, we ask you to revert it within the hour (public_02: your P2 edit at 11:30 was validated at 12:00, failed 0.937 < 0.97, and a revert was requested immediately). It goes through your approval, never behind your back.
- **Wrong:** nothing false. Your policy allows 4 requests/day from 10:00; we chose your stricter numbers.
- **Reply:** "Anita — done: at most 2 requests a day, none before 11:00, each one batching everything we want. We never overwrite your edits; your choice becomes the baseline. One exception you should expect: if a script you ship fails the safety check, we'll ask you (not tell you) to switch back, with the evidence. Caller-ID rotation happens automatically, as your policy allows, and shows in 'changed today' on your screen."

### 05 — Product (Tara)
- **Accepted:** KPIs stay separate; one shared cohort object (canonical segment ids, cells derived from them). Every proposal carries impact, diff, affected leads and a dry run. **Expired = rejected**: we re-plan from fresh data with a new id and never resubmit the stale body. Learning uses only the variant each call actually got. Winners are per-workflow.
- **Min cohort size / window (your open question):** ≥ 300 answered calls per side, a 4-day window for promotion and 2 days for decay. Cost: at ~1,000 answered calls/day, a +15% effect at 10% traffic takes 3–6 days (`reports/false_winner_rate.md` once run). Halving the bar roughly doubles false winners.
- **Declined:** per-person call time as P0. A person gets at most ~8 attempts per campaign, which cannot identify a 12-hour pickup profile. Cohort-level hour weights capture the signal that exists. Caller-ID health and the consolidated view are built.
- **Reply:** "Tara — all definitions held. My answer on cohort size/window: 300 answered calls per arm, a 4-day evaluation window, Holm-corrected; the speed/false-winner trade-off is measured in the repo. I cut per-person call time: ~8 attempts per person can't support it statistically, and a per-person model would mostly fit noise. Call-time optimisation runs per cohort instead, chosen from history. Caller-ID health and the one-screen view are in."

### 06 — CTO (Aryamann)
- **Accepted:** explainable over large. Every change has a `why`. Credits are spent only where they change a decision: 6 credits for the whole public_02 incident, with no LLM call because a production-proven prompt existed. Choices are written down here; the docs are for the on-call engineer (RUNBOOK.md).
- **Reply:** "Built five things properly (ingest, experiments with decay, caller-ID health, incidents, governance + audit) and wrote down what I cut and who I said no to. On every public scenario: zero harmful changes, and a crashed run reproduces a clean one bit for bit."

## 4. Architecture
```
 simulator (HTTP)                                   controller process (single thread)
 ─────────────────                                  ───────────────────────────────────────────────
 /events/chunks/{n} ──► ingest.py  ──(1 tx: cube deltas + leads + config/approval events + cursor)──► SQLite
                          │                                                                         (WAL, one file)
                          ▼ in-memory cubes (cache of SQLite)                                      kv, leads, cubes,
   per workflow per tick: callerid.py ── auto-apply (caller_id only) ──┐                           configs, proposals,
                          incidents.py ── validate → urgent approval ──┤                           decisions (audit),
                          experiments.py (decay every 6 h)  ──┐        │                           exp_state, wf_state,
                          daily plan: experiments.look → cohorts.build │                           alerts, metrics, spend
                                       → planner (diff/dry-run/impact) ┤
                                                                       ▼
                                   governance.py: intent row ─► POST /approvals | PUT config ─► record
                                                  approval_decided event ─► POST /apply ─► record
 /sim/advance ◄──────────────────────────── after decisions
```
**Restart:** `boot()` loads cubes and recent leads from SQLite, then `reconcile()` asks the simulator about every non-terminal proposal: an `intent` row is matched by proposal_id in `GET /approvals` or config history; `applying` is checked via `GET /approvals/{id}`. Ingestion resumes from `last_chunk`. Chunks after the cursor are re-read, so a crash between advance and ingest loses nothing. A simulator that is *behind* the store (a new world) gets a fresh store; the old one is archived.

## 5. The learning loop
- **Cohorts.** Data is kept per lead feature code (252 cells), so any predicate is evaluable afterwards. Candidate segments are single feature values (≥ 3% of traffic) plus contiguous ranges of ordinal features (~20–25). A segment becomes a config cohort only when a rule or harm carve-out needs it. Cells are the coarsest partition respecting all segments in use, capped at 20 (schedule partition dropped first). This avoids fragmenting traffic: on public_02 the largest config has 8 cohorts.
- **Design.** The factorial comes free: the dialler samples welcome and voice independently. Control = incumbent; each challenger gets 10% (40% cap). Comparisons use Mantel-Haenszel, stratified by (day × feature code × other dimension × prompt). Stratifying removes Simpson's paradox once cohorts get different allocations, and keeps interactions with the other dimension from posing as main effects (unit test `test_mh_removes_simpsons_paradox`).
- **Winner.** One look per workflow per day. Holm-Bonferroni over every (dimension, arm, segment) hypothesis: α = 0.02 for whole-population and 0.01 for segments (FWER ≤ 0.03 per look). Also required: ≥ 300 answered calls per side and a point lift ≥ +2%. Replacing an existing winner requires a **head-to-head** win. Overriding a supervisor's explicit choice multiplies p by 10. Displayed lifts are shrunk (normal prior, sd 0.05) so the queue isn't ranked on the winner's curse. **False-winner rate:** `scripts/aa_false_winner.py` measures it through the real `look()` (200 runs × 8 looks); its run was stopped by the memory issue, see §13. The A/A unit test (`test_aa_no_winner_on_identical_arms`) passes, and on public_02 an earlier, looser version (α 0.03, no head-to-head) promoted one false winner. That is why the current thresholds exist.
- **Decay and rollback.** A promoted winner always leaves 20% to control. Every 6 h each live rule is re-tested on the last 2 days; z ≤ −1.96 on ≥ 200 holdout calls triggers a rollback. Arms still under exploration stop on weaker evidence (recent z ≤ −1.645 and > 5% worse), because stopping exploration is cheap to get wrong. Exploration is capped at 6 days.
- **Call time & caller ID.** From history we pick the feature with the largest weighted spread of hour-relative pickup (age band on all public scenarios). Weights = (relative pickup)^1.5, normalised to mean 1 per group, every hour ≥ 0.25. This only re-orders *who* is called. Caller IDs: randomised by the dialler, compared stratified by hour × code, routed ∝ health³ with shrinkage, one 5% probe at a time. A collapse after a number was seen healthy is a sticky "flagged" (spam). A number that was low from its first comparison is "overloaded" (rest 48 h, re-probe).
- **Guardrail.** Engagement-primary workflows: welcome/voice cannot move pickup (assumption 2); caller-ID changes are only made when the estimated pickup effect is ≥ 0. Pickup-primary (KYC): caller-ID and schedule are the levers; welcome/voice still face the same harm stops, so engagement is protected.

## 6. Governance
- **State machine:** `intent → pending → approved → applying → applied`; `pending → rejected | expired`; `approved → lapsed`; `applying → stale` (config moved, e.g. supervisor edit); `intent → stale_submit | invalid`. Auto channel: `intent → applied | refused | stale`. Rejected, expired, lapsed and stale proposals are **terminal**. The next look re-plans from fresh data, fresh base version and a new id.
- **Batching & timing:** one routine proposal per workflow per day, only when *material*: a new winner/stop/rollback, a structural change, or ≥ 10% allocation move. Sent at approver start (+ tenant preference) and not in the last hour. The budget is min(policy, tenant preference) per rolling 24 h, with 1 slot reserved for incidents. We never auto-apply caller IDs while an approval is in flight (it would make that approval stale on apply). Result: zero fatigue rejections and zero apply conflicts on public_01/02.
- **Auto-apply:** only caller IDs, and only where the policy allows. Steps respect `max_shift`. Weights are dyadic so re-normalisation is exact and an auto-apply can never accidentally change another dimension (this happened before the fix). Voluntarily never for welcome, voice or prompt.
- **Impact / affected / dry run:** impact = clip(expected relative primary-KPI lift / 5%, 0, 1); exploration-only requests score ~0, incidents 1.0. Affected leads = still-dialable leads whose dialling changes (next batch only, for `next_campaign_only` workflows). Dry run = the last 2 days of real answered calls re-weighted by each config's per-cohort allocation, using shrunk proven lifts only. Honest about its limits (§13).
- **Supervisor edits mid-experiment** (public_02, nw_renewal D5 15:00, voice → V2): logged, alerted (info), routine proposals paused 24 h, V2 became the control, voice exploration stopped, prior rules retired. The data we had already collected stayed valid.

## 7. Incidents
- **Hallucination:** baseline flag rate = the cleanest prompt with ≥ 2,000 answered calls of history (~1.4%). Trigger: ≥ 150 answered calls on the live prompt in ≤ 4 dialling hours, rate ≥ 2× baseline, Poisson tail p < 1e-4. Expected false positives: ~12 tests/workflow/day × 1e-4 ≈ 1 per workflow per ~2 years. **Plus:** a prompt that a supervisor ships without a track record is validated immediately (n = 300, 3 credits). Engagement regression after any prompt change: stratified, z ≤ −3.5 and < −8%.
- **Fix:** cheapest evidence first. First a previously-live prompt with clean production history; the LLM (3 credits) only if none exists. Validate n = 300 (a 1% failure-rate prompt passes the 0.97 bar ~99.9% of the time). Urgent approval, apply, then verify on ≥ 300 answered calls or reopen.
- **public_02 timeline (ks_cancel):** D2 13:00 caller ID D2 flagged (pickup 0.78× others) → auto-rotated in two `max_shift` steps (13:00, 14:00) → 0% share from D3. D3 11:30 supervisor ships P2 → 12:00 P2 validated (pass 0.937 < 0.97) → incident → P1 validated (0.990) → revert requested → 12:42 approved → 13:00 applied (**MTTR 90 min**) → 17:00 P1 verified clean. 6 credits, 0 LLM calls. Full audit trail: `reports/why_ks_cancel_D3_1300.md`.

## 8. Scale
Measured on public_03_load (12 workflows, ~840k events/day). Measured on a Windows laptop, with controller code from before the last two commits (see §13):

| metric | value |
|---|---|
| ingest | 24k–52k events/s (single thread, json + numpy `add.at`) |
| history bootstrap | 5.5M events in 122 s |
| wall per simulated day | 36–67 s **including the simulator** (~25 s/day of its own); budget 600 s |
| decision time per tick | worst 1.2 s for 12 workflows (daily look) |
| peak RSS | 557 MB |
| ingest lag after each tick | 0 events |
| outcome | +20.8% conclusion, 69 changes, 0 harmful (`reports/public_03_load_grade.md`) |

**At 10× (8.5M events/day, ~100k/hour peak):** JSON parsing in one Python thread breaks first, at ~40k events/s ≈ 140M/hour. That is fine for throughput, but a 10× history bootstrap takes ~20 min. Next: the lead→feature dict (~100 bytes/lead × 3 live days ≈ 600 MB at 10×) and per-chunk SQLite blob writes. Partitioning: workflows share no state except the credit pool, so shard by `workflow_id` (consumer group per shard, one store per shard, credits through a small shared ledger table in Postgres). Swap `json` for `orjson` and add columnar parsing for ~5×.

## 9. Reliability
- **Test:** `python scripts/crash_test.py` (public_02): hard exits at sim minutes 2100, 5040 (mid-incident, right after the supervisor's bad prompt), 5100 and 7800, plus 4 random SIGKILLs (1–6 s wall). Checks run against the simulator: ingested == produced (324,336); 0 approvals applied twice, 0 duplicate proposal ids; every simulator approval known with the same status; 0 rows stuck. **Crashed-run outcome table identical to an uninterrupted run** (`reports/crash_test.md`).
- **What can be lost:** a validation/LLM call interrupted between charge and record may be paid twice; there is no idempotency key on those endpoints. It's logged as an `intent` row in `spend`. Nothing else is lost.
- **Idempotency keys:** deterministic `proposal_id` (`au-<wf>-<kind>-m<minute>-<seq>`); chunk number as the ingestion key.
- **4xx/5xx:** transport errors and 5xx retry with exponential backoff (8 tries, ≤ 30 s). 409 stale → re-plan; 410 lapsed → re-plan; 402 → page; 403 auto-apply → ticket; other 4xx → ticket. The tick continues for other workflows.

## 10. Audit & observability
```
$ python -m controller why --db state/public_02_incidents.db --workflow ks_cancel --at "D3 13:00"
Config v9 was live, applied D3 13:00 by controller_approved (changed: prompt_version).
## The change (approval, proposal au-ks_cancel-incident-m5040-00012)
- summary: Switch script P2 -> P1: new script P2 fails the safety bar: validation val_00001 pass rate 0.937 < 0.97 on n=300
- requested D3 12:00; approval apr_00005 decided D3 12:42 by the client approver (kestrel_finance); status applied, live as v9
- impact score 1.0, affected leads 3635
...
## What happened next (24 h)
- D3 17:00 incident_verified: P1 verified clean after fix (5.0 h after detection)
```
(full output: `reports/why_ks_cancel_D3_1300.md`; supervisor screen: `reports/supervisor_view_public_02.md`)

**Page:** live incident, flagged caller ID, incident fix blocked by budget, no safe prompt candidate, credits exhausted, apply failure, ingest lag. **Ticket, not page:** fatigue rejection (means our accounting is off), refused proposals, auto-apply scope guard. **Never page:** ordinary rejections and expiries, rollbacks, supervisor edits (info). See RUNBOOK.md.

## 11. Results
public_01 (`reports/public_01.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | gap captured |
|---|---|---|---|---|---|---|---|
| ks_cancel | 0.372 | +11.6% | 0.260 | +9.3% | 0.494 | +0.7% | 2% |
| nw_kyc | 0.449 | +23.6% | 0.291 | +20.1% | 0.592 | +0.3% | 6% |
| nw_renewal | 0.459 | +9.3% | 0.282 | +7.1% | 0.556 | +1.9% | 6% |
| ALL | 0.417 | +13.3% | 0.274 | +10.5% | 0.536 | +1.3% | 4% |

18 applied: neutral 10, beneficial 8, harmful 0.

public_02 (`reports/public_02.md`):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.362 | +46.2% | 0.248 | +35.2% | 0.498 | +4.6% | 38% | 106 (330) |
| nw_kyc | 0.413 | +10.2% | 0.262 | +8.1% | 0.599 | +1.2% | −4% | 35 (31) |
| nw_renewal | 0.468 | +9.7% | 0.281 | +7.5% | 0.553 | +0.9% | 10% | 61 (55) |
| ALL | 0.408 | +22.1% | 0.262 | +18.2% | 0.538 | +1.5% | 24% | 202 (416) |

21 applied: neutral 12, beneficial 9, harmful 0. MTTR 90 min.

**Where we helped:** pickup, mostly from caller-ID load-spreading and rotating out bad numbers, plus the prompt incident. **Where we were weak:** engagement-gap capture is low at dev scale (2–10% per workflow). With ~1,000 answered calls/day and 10% arms, a winner needs 3–6 days, and decaying variants decay before they can be proven. On the load scenario, 15× the volume, capture rises to 10–38% per workflow. nw_kyc's −4% gap captured is the cost of exploring options that turned out worse there. **Harmful changes were 0 in the final runs**. Earlier iterations had them, and each one produced a rule now in the code: caller-ID flapping → hysteresis; float noise widening an auto-apply → dyadic weights; re-probing a spam-flagged number → sticky flags; a false winner after a supervisor edit → 10× evidence plus head-to-head.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling / adaptive allocation | Faster learning | Each allocation change costs an approval (2–4/day, latency, 10% judgement rejects); fixed 10% arms + daily looks fit the governance budget. A boosted-arm variant was tried and produced harmful changes, so it was removed |
| Bayesian posterior "P(best) > 0.95" | Intuitive for supervisors | Harder to bound family-wise error across ~150 hypotheses × daily looks; Holm is auditable |
| Auto-apply welcome/voice where allowed (ks) | Speed | Compliance + Anita: customer-facing changes need a human, including rollbacks |
| Redis/Kafka for state | "Real" infra | One file is enough; Postgres-portable schema; no 2 a.m. operator |
| Store raw events | Full audit | 850k/day duplicated from the dialler's log; cubes + config versions answer "why" |
| LLM-first incident fix | It's in the spec | Reverting to a production-proven prompt is cheaper and better evidenced; LLM kept as fallback |
| Per-person preferred hour | Product's headline | ≤ 8 attempts/person can't identify a profile |

## 13. Known weaknesses
- **Measurements and code version:** the public_03 numbers and `reports/crash_test.md` were produced by code from before the last two commits (overloaded-vs-flagged caller IDs; soft exploration stop). public_01/02 reports are from the final code. A final re-run of load + crash test + `aa_false_winner.py` was interrupted (the machine ran low on memory) and still needs to be run: `python scripts/crash_test.py` and `python scripts/aa_false_winner.py --runs 200`.
- Engagement learning is power-limited at dev volume; segment winners (e.g. a voice that helps only ages 46+) are usually not found within 8–10 days. On public_02 the V3-for-46+ effect was missed and V3 was stopped globally.
- Call-time weights are fitted once from history and never verified causally; the dry-run pickup estimate is an explicit upper bound.
- Overloaded vs spam-flagged caller IDs are separated by a heuristic (a step change vs being low from the start).
- The dry run is model-based (proven lifts only), not an off-policy estimate.
- Supervisor view is a CLI, not a UI; alerts aren't wired to a pager.
- Measurements were taken on a Windows laptop, not the 4 vCPU/8 GB Linux reference box.

## 14. If this went to production at Formi next month
1. Shadow mode for a week: ingest real call events, produce proposals, send nothing; compare the dry runs with what actually happened.
2. Move the store to the existing Postgres, ingestion to a stream consumer sharded by workflow, and alerts to the pager.
3. Turn on caller-ID health first (auto-appliable, operational, biggest measured lever), for one tenant.
4. Run A/A tests on live traffic to recalibrate the false-winner rate on real variance before enabling promotions.
5. Ship the supervisor view as a small web page over the same queries; weekly operational-change summary for Northwind.
6. Then incidents (validation on every supervisor prompt change), then experiments, tenant by tenant, with the approvers briefed on what a request will look like.
