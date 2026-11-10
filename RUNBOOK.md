# Controller runbook

For the engineer who gets paged. Everything the controller knows is in one SQLite file
(`state/<scenario>.db`); every command below only reads it.

## Run

```bash
pip install -r requirements.txt
./run.sh http://127.0.0.1:8700            # = python -m controller run --url ... --db auto
CTL_DB=state/x.db ./run.sh <url>           # explicit store
python -m pytest -q tests                  # 26 tests, ~6 s
python scripts/crash_test.py               # kill/restart test, writes reports/crash_test.md
```

Restarting is always safe: `./run.sh` resumes from the store (cursor, learned state, in-flight approvals
are reconciled against the simulator). If the simulator was restarted (a new world), the old store is
archived as `*.old-<ts>` and the controller starts clean.

## Look at it

```bash
python -m controller view   --db state/<scn>.db [--workflow ID]        # the supervisor screen
python -m controller why    --db state/<scn>.db --workflow ID --at "D3 14:00"
python -m controller log    --db state/<scn>.db [--workflow ID] [--kind incident_opened]
python -m controller health --db state/<scn>.db                       # ingest rate, lag, memory, credits, pages
```

## Alerts

| severity | code | meaning | what to do |
|---|---|---|---|
| page | `incident` | hallucination / engagement regression on a live prompt | Check `log --kind incident_opened`. The controller is already proposing a fix; if the approval is stuck outside approver hours, call the client approver. |
| page | `did_flagged_<D>` | a caller ID's pickup collapsed (< 0.7x the others, z <= -4) | Already rotated out. Ask telecom ops to check the number with carriers. The state is sticky: clear it with SQL (below) after remediation. |
| page | `incident_blocked_budget` | a fix is ready but the workflow used its approval budget for 24 h | Call the approver; or wait for the oldest request to age out (the controller retries hourly). |
| page | `incident_no_candidate` | no prompt passes validation | Human needs to pick/author a prompt. |
| page | `credits_low` / `out_of_credits` | credit spend blocked | Top up credits or raise the per-workflow fair-share cap. |
| page | `apply_failed` | apply returned an unexpected error | Check `GET /approvals/{id}`; the controller will re-plan. |
| page | `ingest_behind` | events produced > events ingested after a tick | Look at `health`; usually the simulator returned 5xx for chunks. |
| ticket | `approver_fatigue` | a request was rejected for fatigue: our budget accounting is off | Compare `proposals` with `GET /approvals?workflow_id=`. |
| ticket | `invalid_proposal`, `auto_scope`, `api_*` | a request was refused | Read the message; usually a policy change on the workflow. |
| info | `supervisor_edit_v<N>` | a client supervisor edited config | Nothing. Routine proposals pause 24 h on that workflow. |

Not paged, by design: approval rejections and expiries (normal; the controller re-plans with fresh
data), decay rollbacks (the system working), auto-apply refusals (logged as tickets only if repeated).

## Common surgery

```sql
-- re-admit a caller ID after carrier remediation (it will be probed at 5% first)
UPDATE wf_state SET data = json_set(data, '$.did.status.D2.status', 'unknown') WHERE wf = 'ks_cancel';
-- stop all routine proposals for a workflow (incidents still run)
UPDATE wf_state SET data = json_set(data, '$.supervisor_cooldown_until', 999999) WHERE wf = 'nw_kyc';
```
Stop the controller before editing the store (single-writer).

## Extend

* New detector: add a function next to `incidents.detect`, call it from `Controller.incident()`, and
  route its fix through `Controller.request()` so it gets diff/dry-run/impact and governance for free.
* New lever (e.g. prompt switchback tests): it must produce a full config and go through
  `Controller.request()` (approval) or `Governance.auto()` (only dims the policy allows, and only
  operational ones -- see `CUSTOMER_FACING` in loop.py).
* All thresholds live as module constants at the top of `experiments.py`, `callerid.py`,
  `incidents.py`, `schedule.py`.
