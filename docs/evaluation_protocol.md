# Evaluation Protocol

How the standard scenario is built and scored. It is versioned, seeded and reproducible: a
rebuild is byte-identical (checked by sha256 manifests), so any party — including the jury with
their own held-out attacks — can re-run it.

## Data

- WegenerNet climate station network Level 2 data v8.0 (2007–2024), Feldbach Region, 5-min, UTC.
  Fuchsberger, Kirchengast, Bichler (2025), doi:10.25364/WEGC/WPS8.0-2025.1, CC BY 4.0.
- Variables: air temperature (°C), relative humidity (%), precipitation (mm/5 min). Period 2023–2024.
- **Exclusions:** station 18 (100 % interpolated, decommissioned); station 156 for precipitation
  (no gauge). 154 stations remain.
- **Calibration year:** 2023 is never faulted — detectors warm up and calibrate on it; all faults
  are injected in 2024.
- σ_ref per station = 1.4826·MAD(station − median of 5 nearest neighbours), computed on 2023.
- Gap-filled values (provider DP flag > 0) are excluded from fault windows (≤ 5 % allowed) and
  reported separately in the false-alarm rate. Precipitation is only faulted in months 4–10
  (it is 31–56 % gap-filled Nov–Feb).

## Suites

| Suite | Purpose | Size |
|---|---|---|
| `smoke` | fast iteration | 28 scenarios (T only, 20 stations, June) |
| `dev` | development / tuning | 342 scenarios, dev stations (108) |
| `heldout` | generalisation | 178 scenarios, held-out stations (46), + heldout-only families |
| `clean2024` | false-alarm rate | 2024 untouched, 0 labels |
| `showcase` | demo + figures | 15 scenarios (one per family) |
| `crosssource` | remote-sensing result | 2 whole-network bias scenarios |
| `records_bc` | L1 verification | signed-record stream with B/C faults |

Station split is a seeded permutation (seed 2026): 108 dev / 46 held-out, interspersed through
the same area. Grids (per-family size × duration × replicate) are in [standard-scenario-design.md §6](standard-scenario-design.md) and
`attacks/scenarios/grid_*.yaml`. **The held-out suite uses different stations, different sizes and
two fault shapes never used in development, and is scored once per release — never used for tuning.**

## Metrics (all on 2024)

- **Event detection:** a scenario is detected if any targeted (station, variable) cell alarms in
  [start, end + 1 h). Latency = first such alarm − start (and − onset for drift).
- **Point metrics:** precision / recall / F1 over (time, station) cells, no point-adjustment.
- **Release leakage (headline):** share of faulted cells released as *trusted* (not alarmed, or
  L4 state = trusted) — the brief's "a corrupted observation reaches the analyst", with mean leaked
  magnitude. This is the number that maps a miss to a wrong decision for the analyst.
- **False alarms:** on `clean2024`, alarms per station-day, overall and excluding gap-filled cells
  (lower false-alarm rate is the competition's 2nd tie-breaker).
- **Sensitivity / MDM90:** recall by family × size; smallest size reaching recall ≥ 0.9.
- **Escape magnitude (evasion):** the largest size a detector fails to catch, swept per family
  against the detector's own alarms (`eval/escape.py`). `mdm90` for `rain_scale`/`gain` is on the
  raw factor (identity 1.0), not the departure from 1 — read it as "smallest factor caught".
- **Records (B/C):** per fault type, share of faulted records whose verifier status matches the
  expected status; false positives on the clean stream must be 0 (a legitimate `gap` on the record
  after a rejected one is excused).

## The cross-source result

A `network_bias` fault shifts the whole network together, which neighbour consistency cannot see.
With in-situ data alone it is inseparable from a real regional weather event, so an **independent**
source is required — ERA5-Land reanalysis (or satellite LST). `data_prep/reference.py` provides it
(CDS key → ERA5; else documented) and `eval/baselines.py:crosssource_baseline` consumes it. This is
why the challenge combines remote sensing (GRSS), signal processing (SPS) and security analytics (CIS).

## Difficulty calibration

On the real `dev` suite the reference baseline's offset recall rises 0.31 (k=0.5) → 0.81 (k=5),
a graded curve: small faults mostly evade, large mostly caught, leaving headroom for the team
detector. See `docs/results.md`.

## Reproduce

```bash
uv sync
export WEGENERNET_SHARE_URL=...       # from the portal, Data download → Cloud access
uv run python -m data_prep.download
uv run python -m data_prep.build
uv run python -m attacks.build_suites        # writes data/suites/*, each with a sha256 manifest.json
uv run python -m eval.run_baselines --suite dev
uv run python -m eval.run --suite dev --alarms alarms/baseline_combined/dev
```

A detector exposes `detect(values: dict[str, DataFrame]) -> dict[str, DataFrame]` (bool alarm
tables, same shape as the suite). Score any run — including jury-provided held-out data delivered
as value tables + a labels table in this schema — with `eval.run`. Rebuild determinism is checked
by comparing each suite's `manifest.json` `outputs` hashes.
