# Jury guide: testing the model

This guide shows how to install the project, check it, reproduce the detector's results, plant your own
attack, and run the whole system with its dashboard. It also explains every output.

## What the model does

The **Layer 3 detector** looks for false data injected into weather measurements: someone changing the
values a sensor reports, even when the message is correctly signed (a compromised or physically spoofed
sensor). It works on the WegenerNet network (Feldbach region, Austria), using half-hourly gridded data
for **162 sites** and **3 variables**: air temperature (°C), relative humidity (%) and precipitation
(mm per 30 min).

Every 30 minutes, for every site and variable, it returns:

- a **p-value**: how unusual the reading is compared with clean data. Small means unusual; it is *not* the
  probability of an attack;
- an **alert**: yes or no, from a per-variable threshold chosen on 2023 validation data and then frozen.

Internally it computes 37 features per reading using only past values (the change since the last
reading, the gap to a forecast, to the seasonal pattern and to neighbouring sites, physical-range rules,
…) and scores them with one Extra Trees model per variable. The detector is described in full in
[core/docs/README.md](../core/docs/README.md).

Around it, **Layer 1** proves each reading's origin with Ed25519 signatures, and **Layer 4** combines the
Layer 1 verdict with the detector's alerts into a trust state per sensor, decides which values may be
released, and keeps a tamper-evident audit log ([docs/layer4.md](layer4.md)).

## Requirements

| | Tested with |
|---|---|
| OS | Linux (Ubuntu); the code also has Windows instructions in `docs/windows-*.md` |
| Python | 3.14.4 and 3.13 (clean install re-checked on 3.13) |
| CPU / RAM | 8 cores, 14 GB RAM. Training peaks at 3.5 GB, evaluation under 1 GB; no GPU, no cloud service |
| Disk | about 20 GB with the data (9.3 GB of NetCDF files plus the zips) |

## 1. Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate                      # Windows: .venv\Scripts\activate
python -m pip install --require-hashes -r requirements.txt
python -m pip install -r requirements-detector.txt
python -m pip install -r requirements-dashboard.txt -r requirements-wegenernet.txt   # dashboard and full system
```

## 2. Check that everything works (no data needed, about 20 s)

```bash
python -m pytest -q tests core/tests
```

Expected: **745 passed, 17 skipped**. The 17 skipped tests need a local Mosquitto broker
(`--run-broker`). The detector's own 19 tests are in `core/tests/test_detector.py`; they check, among
other things, that changing a future value never changes an earlier feature (no look-ahead) and that
attack labels never reach the detector's inputs.

Then measure the detector's latency and memory (no data needed, about 20 s):

```bash
python -m eval.detector_overhead
```

It loads the shipped model and prints, among others, `live_tick.median_ms` (time to score one new
30-minute step for all 486 streams) and `peak_rss_mb`. The output is saved to `reports/detector-overhead.json`.

## 3. Get the data

Download the **WegenerNet Level 2 v8.0 (release 2025.1) half-hourly gridded data** for 2023 and 2024
(`WN_L2_HHD_v8_2025_1_UTM_TF1_UTC_2023.zip` and `…_2024.zip`, about 4.7 GB each; free registration at
<https://wegenernet.org/portal>; doi:10.25364/WEGC/WPS8.0-2025.1). Unzip them into `data/`:

```bash
cd data
unzip WN_L2_HHD_v8_2025_1_UTM_TF1_UTC_2023.zip -d WN_L2_HHD_v8_2025_1_UTM_TF1_UTC_2023
unzip WN_L2_HHD_v8_2025_1_UTM_TF1_UTC_2024.zip -d WN_L2_HHD_v8_2025_1_UTM_TF1_UTC_2024
cd ..
```

Each folder must contain 12 monthly files such as `WN_L2_HHD_v8_UTM_TF1_UTC_2023-01.nc`.

## 4. The trained model

The trained detector is **included in the repository** in `artifacts/`:

| File | Content |
|---|---|
| `feature_extractor.joblib` (+ `.json`) | Feature normalisation learned on clean Jan–Aug 2023, with its library versions |
| `extra_trees.joblib` | The three Extra Trees models (one per variable, 120 trees each) |
| `calibration.npz` | Clean Sep–Oct 2023 scores used to turn a score into a p-value |
| `policy.json` | The frozen alert policy, its Nov–Dec 2023 results, and the SHA-256 of the three files above |

`Detector()` checks those SHA-256 hashes and refuses to run if any file changed; the loader also refuses
files saved with a different numpy or scikit-learn version (install `requirements-detector.txt`).

Run the pipeline once to build the data cache, reuse the shipped model, and evaluate 2024
(about 6 minutes: 2 min 20 s extraction, 4 min evaluation):

```bash
python -m core.detection.pipeline
```

It runs six stages and skips any stage whose output already exists, so with the shipped model only
`extract` and `evaluate` run:

| Stage | Data | What it does | Output |
|---|---|---|---|
| `extract` | NetCDF 2023–2024 | Validates units, time steps and grid; keeps every 15th cell (1.5 km spacing) with ≥ 95 % clean training data → 162 sites | `data/cache/` |
| `features` | Jan–Aug 2023 | Learns the feature normalisation from clean data | `artifacts/feature_extractor.joblib` |
| `train` | Jan–Aug 2023 + synthetic attacks | Fits one Extra Trees model per variable | `artifacts/extra_trees.joblib` |
| `calibrate` | Sep–Oct 2023, clean | Stores the clean score distribution that turns scores into p-values | `artifacts/calibration.npz` |
| `select` | Nov–Dec 2023 + attacks | Chooses the alert threshold per variable under a false-alarm budget, then freezes it | `artifacts/policy.json` |
| `evaluate` | 2024 (never seen before) | Replays the year clean and with attacks from two seeds | `outputs/` |

**To retrain from scratch** (about 13 more minutes, peak 3.5 GB RAM), add `--force`. This overwrites
`artifacts/`; training is deterministic, so you get the same policy, calibration and results
(`git checkout artifacts/` restores the shipped files). `--stage NAME` runs a single stage.

## 5. Reproduce the 2024 results

After `evaluate`, `outputs/RESULTS.md` summarises the run. The reference values are committed in
`core/results/`. Training is deterministic: retraining from scratch on the machine above reproduced the
same alert policy, the same calibration and every 2024 metric exactly:

| Attack seed | Precision | Recall | F1 | False-alert episodes per stream-day (no attack) |
|---|---:|---:|---:|---:|
| 91000 | 57.1 % | 31.8 % | 0.408 | 0.0132 |
| 191000 | 59.6 % | 35.3 % | 0.443 | 0.0132 |

Attacks detected at least once (event recall, both seeds): freeze 100 %, invalid range 100 %,
mimicry 89–100 %, noise 96 %, flatten 93–96 %, multivariate 89–96 %, bias 85–93 %, pattern 81–85 %,
drift 78–93 %, patch 69–73 %, spike 31–50 %, rain scaling 15–26 %.

Recall counts every attacked half-hour, so a long attack with one alert per three steps scores about 33 %
even when it is detected. Three families (pattern, mimicry, multivariate) were never seen in training;
the 2024 attacks also use amplitudes not used in training.

To compare your run with the reference:

```bash
python -c "import json; a=json.load(open('outputs/metrics.json')); b=json.load(open('core/results/metrics_2024.json')); print(all(a[s]==b[s] for s in ('nominal','seed_91000','seed_191000')))"
```

## 6. Plant your own attack

`scripts/try_detector.py` takes one 14-day window of 2024, injects the attack you describe, and scores
both the attacked and the clean data (so any alert outside the attack is a false alarm). About 10 s.

```bash
python -m scripts.try_detector                                          # +3 °C bias for 12 h
python -m scripts.try_detector --family drift --amplitude 4 --duration 48
python -m scripts.try_detector --family freeze --variable relative_humidity_pct --site 40
python -m scripts.try_detector --family rain_scale --variable precip_mm_30min --amplitude 3 --start 233
python -m scripts.try_detector --csv local/try.csv                      # also save every scored point
```

| Option | Meaning | Default |
|---|---|---|
| `--family` | `bias`, `drift`, `spike`, `freeze`, `noise`, `flatten`, `rain_scale`, `invalid_range` | `bias` |
| `--variable` | `temperature_c`, `relative_humidity_pct`, `precip_mm_30min` | `temperature_c` |
| `--amplitude` | Size in the variable's unit (bias, drift, spike, invalid range), noise standard deviation, or rain factor | `3` |
| `--site` | Site index 0–161 (`data/cache/selection.parquet` lists them) | `10` |
| `--episode` | 14-day window of 2024, 0–26 | `5` (11–25 March) |
| `--start`, `--duration` | Attack start and length in 30-minute steps; start ≥ 48 (the first 24 h are warm-up) | `120`, `24` |

Example output (default command):

```
Episode 5: 2024-03-11 00:30 -> 2024-03-25 00:00 UTC, 162 sites, 3 variables
Attack: bias 3 degC on temperature_c at site grid_y008_x158 (index 10), steps 120-144 = 2024-03-13 12:30 -> 2024-03-14 00:00 UTC
Points actually changed by the attack: 24 of 24

Attacked stream (this site, this variable):
  alerts during the attack : 22 of 24 steps (92 %)
  detected                 : YES, first alert after 0 min (step 120)
  lowest p-value in window : 2.40e-06 (alert when p <= 0.003, rule 'direct')

Everywhere else (false alarms):
  with the attack present  : 31 alerts on 303240 scored points
  same episode, no attack  : 31 alerts on 303264 scored points
```

Different families behave differently. The default bias is caught at its first half-hour; a 4 °C drift over
24 h (`--family drift --amplitude 4 --duration 48`) is caught only after 14 h, when the ramp has grown
large, with alerts on 16 of 48 steps. That is why recall is lower for slow attacks.

The last two lines show the false-alarm level: 31 alerts in 14 days across 162 sites × 3 variables
(about 0.01 % of points), unchanged by the attack. If an attack changes nothing (for example, scaling rain
during a dry spell), the script says so and suggests another window.

## 7. Run the whole system (Layers 1 → 4) and the dashboard

```bash
python -m scripts.demo_layer4                                    # about 5 s
python -m dashboard.app --db local/layer4-demo.sqlite3           # open http://127.0.0.1:8000
```

The demo signs and verifies two days of readings for a 3 × 3 grid of sensors with the real Layer 1 code,
including a +3 K bias on one sensor (valid signatures), three forged messages on another and one replayed
message on a third. **Its Layer 3 input is a synthetic stand-in** (labelled as such on the dashboard), so
it shows Layer 4's behaviour rather than the detector's accuracy. On the dashboard you can see the biased
sensor go suspicious, then quarantined, then recover; the forged sensor wait in quarantine for an analyst
release; and the replay flagged without quarantine. The dashboard pages are described in
[docs/layer4.md](layer4.md#dashboard).

## Output reference

### The detector in Python

```python
from core.detection.model import Detector
from core.detection.data import load_partition, episodes

detector = Detector()                                    # loads and verifies artifacts/
_, episode = next(episodes(load_partition("test")))      # 14 days of 2024: values [672 steps, 162 sites, 3 variables]
p, alerts = detector(episode)
```

| Output | Shape | Meaning |
|---|---|---|
| `p` | `[time, site, variable]`, float32 | Calibrated p-value: share of clean Sep–Oct 2023 scores at least as high, `(1 + #clean ≥ score) / (n + 1)`. `NaN` where the point cannot be scored (first 48 steps of an episode, missing or flagged data, incomplete history) |
| `alerts` | same, bool | `True` when `p ≤ alpha` for that variable, with the confirmation rule applied |

The frozen alert policy (`artifacts/policy.json`):

| Variable | alpha | Confirmation |
|---|---:|---|
| temperature | 0.003 | direct: alert on the point itself |
| humidity | 0.001 | two of three: the point and one of the two previous points |
| precipitation | 0.0003 | direct |

The order of variables is always temperature, humidity, precipitation. Times are UTC interval ends
(`episode.times`); `episode.sites` lists site IDs (`grid_yYYY_xXXX`) and coordinates.

### Files written by `evaluate` (`outputs/`)

| File | Content |
|---|---|
| `RESULTS.md` | Frozen policy, development results (Nov–Dec 2023) and the 2024 table |
| `metrics.json` | Counts and metrics for `nominal` (clean 2024), `seed_91000` and `seed_191000`, plus timing under `resources` |
| `variables.csv` | The same metrics per suite and variable |
| `families.csv` | Per suite and attack family: `events`, `event_recall` (share of attacks with at least one alert), `point_recall` (share of attacked points alerted) |
| `threshold_search.csv` | Written by `select`: every alpha and confirmation rule tried on Nov–Dec 2023 and whether it met the budget |

Metric definitions (`core/detection/metrics.py`). A point is one (time, site, variable); the first 48 steps
of each episode are excluded:

| Metric | Definition |
|---|---|
| `precision` | `tp / (tp + fp)` |
| `recall_end_to_end` | `tp / positive`: attacked points that were alerted, unscored ones counting as missed |
| `recall_conditional` | `tp / (tp + fn_scored)`: the same among scored points only |
| `f1` | Harmonic mean of precision and end-to-end recall |
| `false_positive_rate` | `fp / negative_scored` |
| `false_episodes_per_stream_day` | Runs of consecutive false alerts (counted once) per 48 clean steps of one site-variable. Budget: ≤ 0.05 |
| `coverage` | Scored points / eligible points (required ≥ 95 %) |
| `injected_prevalence` | Share of eligible points changed by an attack |

### `try_detector --csv`

One row per scored point: `time`, `site_id`, `variable`, `observed` (value after the attack), `clean`
(original value), `attacked` (whether the attack changed this point), `p_value`, `alert`.

### Layer 4 decisions

Per sensor and tick, a `TrustDecision`: `state` (trusted / suspicious / quarantined), `cause`
(provenance or anomaly), `category` (`provenance_failure` or `authentic_but_suspicious`, reported
separately, plus `nominal` and `unscored`), `actions` (flag, quarantine, impute, notify), alert counts, an
`explanation` sentence, and per variable the Layer 1 status, raw value, p-value, alert and the value
released downstream (`raw`, `imputed` from trusted neighbours, or `withheld`). See
[docs/layer4.md](layer4.md) and [docs/layer4-thresholds.md](layer4-thresholds.md).

## Limitations

- **Gridded data, synthetic attacks.** Grid cells are interpolated, not independent sensors, and the
  attacks come from the project's own generator (with unseen families and amplitudes in 2024).
- **Weak families:** short spikes (31–50 % detected) and rain scaling (15–26 %). Scaling rain consistently
  needs an independent reference such as reanalysis or satellite precipitation, which is not in the data.
- **Station suite not graded yet.** The team's standard attack suite (`docs/evaluation-suite.md`) uses
  5-minute station data; the detector must be trained on station data before it can be scored there.
- **Layer 4 thresholds** come from a simulation based on these 2024 results (`docs/layer4-thresholds.md`).

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `No WegenerNet NetCDF source files found` | The folders are not in `data/` with the names in step 3 (`source_glob` in `config/detector.yaml`) |
| `Extraction config changed; use --force to rebuild` | `config/detector.yaml` changed since the cache was built: `python -m core.detection.pipeline --stage extract --force` |
| `Library versions differ from the saved extractor` | The model was saved with another numpy or scikit-learn: install `requirements-detector.txt` or retrain |
| `Frozen artifact changed: …` | A file in `artifacts/` no longer matches `policy.json`: `git checkout artifacts/`, or retrain with `--force` |
| `No such file … policy.json` | `artifacts/` is missing: `git checkout artifacts/`, or retrain with `--force` |
