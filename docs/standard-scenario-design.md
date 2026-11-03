# Standard Scenario & Evaluation Suite — Design (Track 2, False-Data Injection)

Date: 2026-10-27

## 1. Intent

The team's prototype (L1 signatures → L2 features → L3 detectors → L4 trust) is judged on how well it catches
false data. This workstream builds the **standard scenario**: a versioned,
seeded, reproducible set of labelled fault scenarios injected into real WegenerNet data, plus the scoring that
turns detector output into the numbers the jury asks for (detection quality, false alarms, latency).

Score weight riding on this workstream: Phase 1 "Detector design & evaluation" (20) + "Standard scenario & evasion" (20);
Phase 2 "Held-out standard attack & evasion" (20, first tie-breaker) — and false-alarm rate is the second tie-breaker.

## 2. Goals

- **G1 Reproducible standard scenario.** YAML config + seed → byte-identical suites, proven by a sha256 manifest.
- **G2 Coverage of the teammates' attack table.** Every type in `Architecture and attacks.pdf` maps to an implemented
  family or is listed as deferred (§5).
- **G3 Graded difficulty.** Fault sizes are expressed in units of each station's normal disagreement with its
  neighbours (σ_ref), so results become *recall-vs-size curves*, not single numbers.
- **G4 Held-out generalisation.** A held-out suite uses different stations, different sizes/durations and two fault
  shapes never seen in development — a rehearsal of the jury's held-out test.
- **G5 Honest false alarms.** A full fault-free 2024 suite measures false alarms; provider gap-filled values are
  reported separately.
- **G6 Report-ready outputs.** Catalogue, protocol, figures and tables drop straight into the Phase 1 report.

## 3. Non-goals / anti-goals

- Non-goal: building detectors, signer/verifier, dashboard (teammates).
- The "evasion" criterion is met by three things: held-out variants (different stations/sizes/shapes), the
  escape-magnitude search against the detector, and the statistics-matched / cycle-shaped / network-wide
  families that each defeat one detector family so the others must carry the catch.
- Anti-goal: anyone tuning detectors on the held-out suite.
- Anti-goal: injected values outside physical range (RH > 100 %, negative rain) — trivially caught, unrealistic.
- Anti-goal: point-adjusted metrics (they inflate scores); committing raw data or the cloud share link;
  anything identifying the team (Phase 1 anonymity).

## 4. Data (verified 2026-10-27)

- WegenerNet L2 v8, 5-min, UTC, Feldbach Region station IDs ≤ 499, 2023-01-01 00:00 → 2024-12-31 23:55
  (210 528 steps). Variables: `air_temperature` (degC), `relative_humidity` (%), `precipitation` (mm per 5 min).
- Already built: `data/clean/{var}.parquet` (time × station float32), `{var}_flag.parquet` (DP flag int8:
  0 measured, 1 time-interp, 2 neighbour-interp, 3 grid, −1 absent), `stations.csv`.
- **Exclusions:** station 18 (100 % interpolated, decommissioned); station 156 for precipitation (no gauge).
- **Coordinates:** use the location whose validity interval contains 2024-01-01T00:00Z (stations 83, 84, 136 moved).
- **Gap-filled share:** T 1.2 %, RH 1.5 %, precipitation 18.1 % — precipitation is 31–56 % filled Nov–Feb,
  ≤ 5 % Apr–Oct. ⇒ precipitation faults only in months 4–10.
- Nearest-neighbour spacing median 1.15 km (max 1.79 km).
- **σ_ref** = 1.4826·MAD(x_station − median of its 5 nearest neighbours) over 2023:
  T median 0.59 °C (p10 0.36, p90 1.08); RH median 2.45 % (p10 1.60, p90 3.78).
- 2023 = calibration year, always fault-free. All faults go in 2024.

## 5. Scenario families

**Category A — value faults (signature stays valid; L2/L3 must catch).** Applied to T and RH unless noted.

| Family | Effect on the window | Size parameter | Teammates' type |
|---|---|---|---|
| `offset` | x + k·σ_ref | k | Bias / offset |
| `drift` | x + linear ramp 0 → k·σ_ref over the window | k, ramp length | Slow drift |
| `spike` | n isolated single-step pulses of ±k·σ_ref | k, n | Spike / pulse |
| `noise` | x + N(0, (k·σ_ref)²) | k | Noise (inflate) |
| `flatline` | rolling mean over w (variance suppressed) | w | Flatten |
| `stuck` | last pre-window value held | — | Stuck-at / freeze |
| `multi_offset` | same k·σ_ref offset on a station + its n−1 nearest neighbours | k, n | Colluding neighbours |
| `gain` *(held-out only)* | window mean + g·(x − window mean) | g | — (unseen shape) |
| `intermittent_offset` *(held-out only)* | offset k·σ_ref on alternating 1-h blocks | k | — (unseen shape) |
| `rain_scale` | precipitation × f | f | Bias (multiplicative) |
| `rain_suppress` | precipitation → 0 during a wet window | — | Stuck-at 0 |
| `rain_inject` | adds a triangular event of total R mm over D when station and 5-NN are dry | R, D | Spike / pulse |
| `realistic_replace` | window resampled from the station's own 30-day history (marginal stats preserved) + k·σ_ref bias | k, generator | Synthetic mimicry |
| `cycle_shaped` | bias k·σ_ref modulated by the station's diurnal amplitude (rides inside the daily cycle) | k | Pattern masking |
| `value_replay` *(heldout)* | verbatim copy of the station's window from `days_back` days ago + noise·σ_ref | days_back, noise | Replay (value-level, strong evasion) |
| `network_bias` *(held-out)* | k·σ_ref offset on **all** stations in the pool at once (defeats neighbour consistency) | k | Colluding neighbours (network-wide) |

The last three all remain synthetic perturbations of public data, scored by the same harness to quantify where the detector is weak. `realistic_replace` and `cycle_shaped` are designed so that spatial/cross-source detectors still catch them; `network_bias` is the case only cross-source catches, and motivates the remote-sensing component.

**Category B/C — record faults (L1 must catch):** `tamper_value`, `replay`, `drop`, `reorder`, `shift_timestamp`,
`rewrite_stored`, `unknown_sensor`, each with an expected L1 status from {bad_signature, replay, gap,
out_of_window, unknown_sensor}.

**Adaptive-difficulty evaluation:** not a new family — a search that reports, per base family, the smallest magnitude the detector fails to catch (the blind-spot boundary). This is the "evasion" result, framed as a robustness number.

Physical clipping after injection: RH ∈ [0, 100]; precipitation ≥ 0; T unclipped.

## 6. Suites

| Suite | Purpose | Contents |
|---|---|---|
| `smoke` | fast iteration for teammates | ~30 scenarios, T only, 20 stations, June 2024 |
| `dev` | development / tuning | ~340 base specs (pre-placement), dev stations (70 %), dev grid |
| `heldout` | generalisation rehearsal | ~180 base specs + held-out-only families (gain, intermittent_offset) + Tasks 15/16/18 additions, held-out stations (30 %) |
| `clean2024` | false-alarm rate | 2024 untouched |
| `showcase` | demo + figures | one hand-written scenario per family |
| `records_bc` | L1 verification | signed-record streams with B/C faults |
| `crosssource` | remote-sensing result | `network_bias` scenarios |
| `adaptive_probe` | evasion/robustness | per-family escape-magnitude scenarios vs a given detector |

Grids (σ_ref units unless stated). Each row gives the swept parameter list(s) and the window duration; `expand` is the Cartesian product of the parameter list(s) × the durations × replicates × applicable variables. Replicates: dev 2, dev-precipitation 3, heldout 2, smoke 1. Value families (`offset, drift, spike, noise, flatline, stuck, multi_offset`) apply to `air_temperature` and `relative_humidity`; `rain_*` to `precipitation`; `gain, intermittent_offset` are **heldout-only**; `network_bias` is heldout-only; `realistic_replace, cycle_shaped` have their own grids in `attacks/scenarios/grid_*.yaml`.

| Family | dev params | dev duration(s) | heldout params | heldout duration(s) |
|---|---|---|---|---|
| `offset` | k = 0.5, 1, 2, 3, 5 | 1 h, 6 h, 24 h, 72 h | k = 0.75, 1.5, 4 | 12 h, 48 h, 120 h |
| `noise` | k = 0.5, 1, 2, 3, 5 | 1 h, 6 h, 24 h, 72 h | k = 0.75, 1.5, 4 | 12 h, 48 h, 120 h |
| `drift` | k = 1, 2, 3, 5 | ramp = window = 24 h, 72 h, 168 h | k = 1.5, 4 | 120 h, 240 h |
| `spike` | k = 2, 4, 8 ; n = 1, 3, 6 | 24 h | k = 3, 6 ; n = 2, 4 | 48 h |
| `flatline` | w = 1 h, 3 h, 6 h | window = w | w = 2 h, 4 h | window = w |
| `stuck` | — | 1 h, 6 h, 24 h, 72 h | — | 12 h, 48 h |
| `multi_offset` | k = 2 ; n = 2, 3, 5, 8 | 24 h | k = 3 ; n = 4, 6 | 48 h |
| `gain` | — | — | g = 0.5, 1.5, 2 | 24 h, 72 h |
| `intermittent_offset` | — | — | k = 1.5, 3 | 48 h, 120 h |
| `rain_scale` | f = 0.5, 1.5, 2, 3 | 6 h, 24 h | f = 0.75, 2.5 | 12 h |
| `rain_suppress` | — | 6 h, 24 h | — | 12 h |
| `rain_inject` | total_mm = 2, 5, 10, 20 ; duration_steps = 1 h, 3 h (= window) | — | total_mm = 7, 15 ; duration_steps = 2 h | — |

`spike` and `multi_offset` use a single fixed window; `flatline`, `drift` and `rain_inject` derive their window from the swept size/ramp/duration parameter, so they have no separate duration list. Durations in hours are converted to 5-min steps (×12) by `expand`.

Placement rules: window eligible only if ≤ 5 % of target points are gap-filled; precipitation only months 4–10;
`rain_scale`/`rain_suppress` windows must contain ≥ 2 mm (≥ 5 mm for suppress) at the target; two scenarios on
the same variable conflict if any of their stations are ≤ 3 km apart and their windows ±24 h overlap.
Station split dev/held-out: deterministic shuffle with seed 2026.

## 7. Scoring

- **Event detection:** a scenario is detected if any targeted (station, variable) cell alarms within
  [start, end + 1 h). Latency = first such alarm − start; for drift also from `onset` (first step where the
  injected delta ≥ 1 σ_ref).
- **Point metrics:** precision / recall / F1 over (time, station) cells, no point-adjust.
- **Release leakage (headline):** share of faulted (time,station) cells released as trusted (not alarmed, or state = trusted if L4 emits states) — the brief's "corrupted observation reaches the analyst", with mean leaked magnitude and an optional downstream-impact weighting.
- **False alarms:** on `clean2024`, alarms per station-day, overall and excluding gap-filled cells.
- **Efficiency / security overhead:** detector per-window latency p50/p95, peak memory on `dev`, Ed25519 sign+verify throughput and added bytes per record.
- **Escape magnitude (evasion):** per family, the largest magnitude the detector misses.
- **Sensitivity:** recall by family × size; **MDM90** = smallest size with recall ≥ 0.9 (the residual-risk
  boundary reported as the evasion result).
- **Records:** per B/C type, share of faulted records whose verifier status matches the expected status;
  false positives on the clean stream must be 0.
- **Difficulty check:** with the reference baselines, `dev` k = 5 offsets reach ≥ 0.9 recall and k = 0.5 stay
  < 0.5 — otherwise the grid is re-tuned before release.

## 8. Contract with teammates

- They read: `data/clean/*`, `data/suites/<suite>/{var}.parquet` (same shape as clean), `data/suites/<suite>/labels.parquet`,
  `data/clean/sigma_ref.csv`, `data/clean/neighbors.csv`, `data/suites/records_bc/records.jsonl`.
- They write: `alarms/<run>/<suite>/{var}.parquet` (bool, same index/columns as the suite tables) and, for
  records, `alarms/<run>/records_bc/statuses.jsonl` (`{"idx": int, "status": str}`).
- One command scores a run: `uv run python -m eval.run --suite dev --alarms alarms/<run>/dev`.
- The jury may bring their own held-out data: anything delivered as value tables + a labels table in the
  documented schema scores with the same command.
