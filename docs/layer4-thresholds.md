# Layer 4 thresholds

Layer 4 (`core/response/trust.py`) turns two inputs into a sensor state and a release decision:

- **Layer 1 verdicts** (`ResponseEngine.on_verdict`, the `ProvenancePipeline` L4 sink)
- **Layer 3 output rows** (`time, site_id, variable, p_value, alert`, from `score_stream`)

All thresholds live in `TrustPolicy`. This file explains each value, why it was chosen, and how to
re-derive it. One tick = one 30-minute interval.

## Summary

| Threshold | Value | Meaning |
|---|---:|---|
| `short_window` | 12 ticks (6 h) | Window for the "suspicious" count |
| `suspicious_alerts` | 3 | ≥ 3 alerted ticks in the last 12 → **suspicious** |
| `trusted_exit_alerts` | 0 | Suspicious → **trusted** once the last 12 ticks have no alert |
| `long_window` | 24 ticks (12 h) | Window for the "quarantine" count |
| `quarantine_alerts` | 6 | ≥ 6 alerted ticks in the last 24 → **quarantined** (cause: anomaly) |
| `quarantine_exit_alerts` | 2 | Anomaly quarantine → **suspicious** once the last 24 ticks hold ≤ 2 alerts |
| `provenance_window` | 4 ticks (2 h) | Window for counting Layer 1 failures |
| `provenance_quarantine_failures` | 3 | ≥ 3 failures in the window → **quarantined** (cause: provenance) |
| `min_neighbors` | 2 | Trusted neighbours needed to impute a value; otherwise it is withheld |

A tick counts as *alerted* when Layer 3 alerted on at least one of the sensor's three variables.
Ticks that Layer 3 did not score (warm-up, missing history) are skipped: they neither add an alert
nor push old alerts out of the windows.

## Decisions taken

These were agreed before the values were set:

1. **Alert counts instead of a smoothed score.** A smoothed (EWMA) trust score settles at about
   1 − (fraction of alerted ticks). Layer 3 alerts on about a third of attacked points, so an attack
   kept the score around 0.67 and the sensor oscillated near the "suspicious" line without ever being
   quarantined. Counting alerts in a window lets scattered alerts accumulate, while short false bursts
   stay below the line.
2. **One provenance failure does not quarantine.** Anyone can publish junk under a sensor's id; Layer 1
   rejects it, but quarantining on that would let an attacker silence any sensor without a key. A
   single failure is flagged, notified and audited; the rejected value is never released, and the
   sensor's genuinely signed readings keep flowing. Quarantine needs **3 failures within 2 hours**, or a
   failure **in the same tick as a Layer 3 alert**.
3. **Recovery.** Anomaly quarantine ends automatically when the alerts age out. A provenance quarantine
   requires an **analyst release** (dashboard button or `ResponseEngine.release(sensor, analyst, reason)`),
   recorded in the audit log with the analyst's name and reason.
4. **Gaps are noted, not counted.** A Layer 1 `gap` is not proof of deletion; it appears in the
   explanation only.
5. **Budget:** at most **1 false quarantine per sensor per month** on clean data.

## How the alert-count values were chosen

The values come from `eval/layer4_threshold_sim.py`, a Monte-Carlo simulation fed with Layer 3's
measured 2024 behaviour (`core/results/metrics_2024.json`, `families_2024.csv`):

- **Clean data:** 0.0132 false alert episodes per stream per day, about 2.3 ticks long; three streams
  (variables) per sensor.
- **Attacks:** 12, 48 or 96 ticks long; each attacked tick alerts with probability 0.33 (2024 point
  recall: 32–35 %).

| Rule (suspicious / quarantine) | False suspicious per sensor-month | False quarantines per sensor-month | Attacks quarantined (12 / 48 / 96 ticks) | Median ticks to quarantine (48 / 96) |
|---|---:|---:|---|---|
| Previous EWMA rule (≈ 2 in a row / 4 in a row) | 0.67 | 0.215 | 8 % / 28 % / 52 % | 25 / 41 |
| **3 in 12 / 6 in 24 (chosen)** | **0.41** | **0.072** | **16 % / 100 % / 100 %** | **16 / 16** |
| 4 in 12 / 6 in 24 | 0.22 | 0.072 | 17 % / 99 % / 100 % | 17 / 16 |
| 3 in 12 / 8 in 24 | 0.36 | 0.018 | 2 % / 91 % / 99 % | 22 / 23 |
| 3 in 12 / 10 in 48 | 0.39 | 0.009 | 0 % / 98 % / 100 % | 29 / 28.5 |

**Choice: 3 in 12 and 6 in 24.** It is far inside the budget (0.072 false quarantines per sensor-month,
about one every 14 months per sensor, or roughly two a month across 30 sensors). It quarantines every
simulated 6–48 h attack, with a median delay of 16 ticks (8 h). Short 6 h attacks mostly reach
"suspicious" (83 %) rather than quarantine; their values are already imputed instead of released
while the sensor is suspicious.

What it costs: about 0.41 false "suspicious" episodes per sensor-month (about 12 a month across
30 sensors). These only cause imputation, never quarantine. If analysts find this too noisy,
`4 in 12` halves it with almost no loss on long attacks (see the table).

**Exit values.** `quarantine_exit_alerts = 2` and `trusted_exit_alerts = 0` are lower than the entry
values (6 and 3), so a sensor does not flap in and out of a state on one alert. After an anomaly
quarantine triggered by 6 consecutive alerts, a clean sensor reaches suspicious on the 22nd clean tick
(the long count has fallen to 2) and trusted on the next one, because its short window is already
clean: about 11.5 h in total. A single new alert while suspicious restarts the 12-tick wait.

## How the provenance values were chosen

Genuine operation produces essentially no Layer 1 failures (TLS transport, signed records), so the
window can be generous without adding false quarantines. With a 2-tick (1 h) window, an attacker sending
one forged message every 30 minutes would never reach 3 failures in one window. **4 ticks (2 h)** with
**3 failures** catches a sustained forgery within 2 h while one stray message only flags the sensor.

## Caveats

- **The simulation is a model, not a measurement.** It treats alerts within an attack as independent;
  real alerts cluster, which helps the counts. It uses 2024 Layer 3 results from the reference detector,
  which was trained on `T_z300m_HHM` and its own site grid, not on Layer 2's current variable and cells.
- **Re-derive these values once Layer 3 runs on the deployment data.** Replay the Nov–Dec 2023
  validation scores (clean and attacked) through `ResponseEngine.run`, measure false quarantines and
  time-to-quarantine for each candidate rule, and keep the best rule within the budget. This is the same
  method Layer 3 used to choose its alert threshold.
- **Trust state is held in memory.** A restart resets every sensor to trusted with empty windows; the
  audit log is the only persistent record.
- **`trust_score` is for display.** It summarises the same counts on a 0–1 scale
  (`1 − max(0.5·short/3, long/6)`, scaled down by recent provenance failures, 0 under provenance
  quarantine). The state is decided by the count thresholds, not by the score.

Reproduce the table: `python eval/layer4_threshold_sim.py`.
