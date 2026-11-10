# Why ks_cancel looked the way it did at D3 13:00

Config **v9** was live, applied D3 13:00 by **controller_approved** (changed: prompt_version).

## The change (approval, proposal `au-ks_cancel-incident-m5040-00012`)
- summary: Switch script P2 -> P1: new script P2 fails the safety bar: validation val_00001 pass rate 0.937 < 0.97 on n=300
- requested D3 12:00; approval apr_00005 decided D3 12:42 by the client approver (kestrel_finance); status **applied**, live as v9
- impact score 1.0, affected leads 3635

Rationale:
```
Incident opened 0.0 h ago: new script P2 fails the safety bar: validation val_00001 pass rate 0.937 < 0.97 on n=300. Candidate P1 chosen from production history; validation val_00002 pass rate 0.990 on n=300. If P2 was a supervisor edit, this reverts it -- with your approval only. After apply we verify the flag rate returns to normal.
```
Dry run:
```
{
 "expected_engagement_current": 0.4938,
 "expected_engagement_lift": 0.0,
 "expected_engagement_proposed": 0.4938,
 "expected_pickup_lift": 0.0,
 "method": "replay of real connected calls from the last 2 days, re-weighted by each config's per-cohort allocation; arms not yet proven assumed equal to control",
 "observed_engagement": 0.4938,
 "replayed_connected_calls": 2835
}
```
Diff:
```
{
 "prompt_version": {
  "from": "P2",
  "to": "P1"
 }
}
```
Evidence recorded at proposal time:
- incident_opened: new script P2 fails the safety bar: validation val_00001 pass rate 0.937 < 0.97 on n=300

## What happened next (24 h)
- D3 17:00 incident_verified: P1 verified clean after fix (5.0 h after detection)

KPIs 2 days before vs 2 days after v9: pickup 26.0% -> 24.5%, engagement 49.4% -> 50.5% (descriptive only: other changes overlap; experiment arms are the causal evidence)
