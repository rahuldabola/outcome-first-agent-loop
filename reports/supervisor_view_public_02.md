# Supervisor view - public_02_incidents - as of D10 00:00

## ks_cancel (kestrel_finance) - primary: engagement_rate, guardrail: {"pickup_rate_max_drop": 0.02}

| | pickup | engagement | dials |
|---|---|---|---|
| today D10 | - | - | 0 |
| yesterday | 25.0% | 52.6% | 4800 |
| last 7 days before D0 | 24.4% | 49.8% | 33600 |

**Live config v15** (set by controller_auto at D9 14:00): script P1, 4 cohorts, everyone-else gets welcome W2 80%/W1 20%, voice V1 100%, caller ID D1 56%/D3 44%

**What is being tested and what is winning** (engagement vs control, last 4 days, stratified):

| dimension | option | status | answered calls | engagement | vs control |
|---|---|---|---|---|---|
| welcome_message | W1 | control (holdout) | 1796 | 48.6% | - |
| welcome_message | W2 | WINNING in ALL | 1635 | 52.8% | +9.5% (90% CI +2.2%..+17.4%) |
| welcome_message | W3 | stopped | 0 | - | - |
| voice | V1 | control | 3473 | 50.8% | - |
| voice | V2 | stopped | 0 | - | - |
| voice | V3 | stopped | 33 | 48.5% | -7.0% (90% CI -32.3%..+27.8%) |
| voice | V4 | stopped | 0 | - | - |

**Waiting on you:** nothing

**Changed today:** nothing

**Caller IDs:** D1 healthy, D2 flagged, D3 healthy

---

## nw_kyc (northwind_life) - primary: pickup_rate, guardrail: {"engagement_rate_max_drop": 0.02}

| | pickup | engagement | dials |
|---|---|---|---|
| today D10 | - | - | 0 |
| yesterday | 26.1% | 55.2% | 2000 |
| last 7 days before D0 | 24.1% | 60.1% | 14000 |

**Live config v4** (set by controller_approved at D7 14:00): script P1, 4 cohorts, everyone-else gets welcome W1 100%, voice V1 100%, caller ID D1 90%/D2 5%/D3 5%

**What is being tested and what is winning** (engagement vs control, last 4 days, stratified):

| dimension | option | status | answered calls | engagement | vs control |
|---|---|---|---|---|---|
| welcome_message | W1 | control | 1496 | 59.8% | - |
| welcome_message | W2 | stopped | 77 | 50.6% | -19.0% (90% CI -33.4%..-1.6%) |
| voice | V1 | control | 1467 | 59.1% | - |
| voice | V2 | stopped | 103 | 64.1% | +11.6% (90% CI -3.1%..+28.5%) |

**Waiting on you:** nothing

**Changed today:** nothing

**Caller IDs:** D1 healthy, D2 unknown, D3 unknown

---

## nw_renewal (northwind_life) - primary: engagement_rate, guardrail: {"pickup_rate_max_drop": 0.02}

| | pickup | engagement | dials |
|---|---|---|---|
| today D10 | - | - | 0 |
| yesterday | 29.4% | 56.7% | 3600 |
| last 7 days before D0 | 26.8% | 54.7% | 25200 |

**Live config v7** (set by controller_approved at D6 19:00): script P1, 4 cohorts, everyone-else gets welcome W1 100%, voice V2 100%, caller ID D4 27%/D2 26%/D1 24%/D3 23%

**What is being tested and what is winning** (engagement vs control, last 4 days, stratified):

| dimension | option | status | answered calls | engagement | vs control |
|---|---|---|---|---|---|
| welcome_message | W1 | control | 3169 | 55.8% | - |
| welcome_message | W2 | stopped | 0 | - | - |
| welcome_message | W3 | stopped | 0 | - | - |
| voice | V1 | not tested | 0 | - | - |
| voice | V2 | control | 3169 | 55.8% | - |
| voice | V3 | not tested | 0 | - | - |
| voice | V4 | not tested | 0 | - | - |

**Waiting on you:** nothing

**Changed today:** nothing

**Caller IDs:** D1 healthy, D2 healthy, D3 healthy, D4 healthy

---

