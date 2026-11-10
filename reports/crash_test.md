# Crash-recovery test - public_02_incidents

started 2026-11-11 00:54:44

- hard exit requested at sim minute 2100: controller exit code 17, simulator at minute 2100
- hard exit requested at sim minute 5040: controller exit code 17, simulator at minute 5040
- hard exit requested at sim minute 5100: controller exit code 17, simulator at minute 5100
- hard exit requested at sim minute 7800: controller exit code 17, simulator at minute 7800
- random hard kill #1 after 2.6s wall: simulator at minute 9240
- random hard kill #2 after 1.8s wall: simulator at minute 9540
- random hard kill #3 after 4.3s wall: simulator at minute 12540
- random hard kill #4 after 1.4s wall: simulator at minute 12780
- final restart ran to completion: exit code 0

## Invariants (checked against the simulator)

- PASS: exactly-once ingestion: ingested 324338 events, simulator produced 324338
- PASS: no double apply: 10 approvals applied, 0 applied twice (none), duplicate proposal ids in config history: none
- PASS: no lost approvals: simulator holds 11 approvals, all known to our store (missing none, status mismatches none)
- PASS: nothing stuck mid-flight: 0 proposals in intent/applying
- PASS: controller restarts recorded in the audit log: 8
- PASS: credit calls interrupted mid-flight (may have been charged twice): 0

## Outcome: crashed run vs uninterrupted run

Crashed + restarted:

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.362 | +46.2% | 0.248 | +35.2% | 0.498 | +4.6% | 38% | 106 (330) |
| nw_kyc | 0.413 | +10.2% | 0.262 | +8.1% | 0.599 | +1.2% | -4% | 35 (31) |
| nw_renewal | 0.468 | +9.7% | 0.281 | +7.5% | 0.553 | +0.9% | 10% | 61 (55) |
| ALL | 0.408 | +22.1% | 0.262 | +18.2% | 0.538 | +1.5% | 24% | 202 (416) |
21 applied — neutral: 12, beneficial: 9

Uninterrupted:

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.362 | +46.2% | 0.248 | +35.2% | 0.498 | +4.6% | 38% | 106 (330) |
| nw_kyc | 0.413 | +10.2% | 0.262 | +8.1% | 0.599 | +1.2% | -4% | 35 (31) |
| nw_renewal | 0.468 | +9.7% | 0.281 | +7.5% | 0.553 | +0.9% | 10% | 61 (55) |
| ALL | 0.408 | +22.1% | 0.262 | +18.2% | 0.538 | +1.5% | 24% | 202 (416) |
21 applied — neutral: 12, beneficial: 9

Identical outcome tables: **True**
