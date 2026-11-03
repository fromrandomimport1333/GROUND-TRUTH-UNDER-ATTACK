# Ground-Sensor False-Data Detection — Evaluation Suite

**Track 2: False-Data Injection.** This project takes real weather-station data, fakes parts of
it in many ways (with an exact answer key), and scores how well a detector catches the fakes.
It uses **ground sensor data only** — no satellite images.

> New here? Read `GUIDE.md` for the plain-language tour. This README is the how-to.

---

## 1. Install (once)

You need [`uv`](https://docs.astral.sh/uv/) (Python manager). Then:

```bash
cd <project-folder>
uv sync          # installs everything into .venv
```

Check it works — this needs no data and takes ~10 seconds:

```bash
uv run pytest -q
```

Expect **`238 passed`**. If you see that, the whole system is sound.

---

## 2. Get the data (once)

1. Register (free) at <https://wegenernet.org/portal>.
2. Go to **Data download → Cloud access**, and copy the "WegenerNet Cloud Archive" link.
3. Paste it below and run:

```bash
export WEGENERNET_SHARE_URL="https://cloud.uni-graz.at/s/XXXXXXXX"   # your link
uv run python -m data_prep.download    # ~430 MB  -> data/raw/
uv run python -m data_prep.build       # cleans it -> data/clean/
```

You now have clean tables of temperature, humidity and rain for 154 stations, 2023–2024.

---

## 3. Build the exams (the fake-data test sets)

```bash
uv run python -m attacks.build_suites
```

This writes six test sets to `data/suites/`:

| Exam | What it's for |
|---|---|
| `smoke` | 28 fakes — quick check while developing |
| `dev` | 342 fakes — the main set to tune the detector on |
| `heldout` | 210 fakes with unseen stations/sizes/shapes — the "secret" test |
| `clean2024` | the year with **no** fakes — to measure false alarms |
| `showcase` | one of each fake type — for the demo and figures |
| `crosssource` | the whole-network fake (needs an outside reference) |

Each folder also gets a `manifest.json` fingerprint; rebuilding gives identical files.

---

## 4. Plug in the model (the detector)

Your detector must be one Python function with this exact shape:

```python
# e.g. in witness/detector.py
import pandas as pd

def detect(values: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Input: {'air_temperature': table, 'relative_humidity': table, 'precipitation': table}
       Each table is time (rows) x station (columns) of readings.
       Return: the SAME keys/shape, but boolean — True where you flag a fake."""
    alarms = {}
    for name, table in values.items():
        alarms[name] = your_model_flags(table)   # <-- your logic; must be True/False, same shape
    return alarms
```

Rules of thumb:
- Same rows and columns in, same rows and columns out — just `True`/`False`.
- `True` = "I think this reading is faked."
- You may train/calibrate on **2023** (it is never faked); the fakes are all in **2024**.

Save the detector's alarms for a suite as one file per measurement:
`alarms/<yourname>/<suite>/air_temperature.parquet`, etc. (same shape as the suite tables).

---

## 5. Score the model

```bash
uv run python -m eval.run --suite dev --alarms alarms/<yourname>/dev
```

Output goes to `results/...` and reports:
- **catch rate** (recall) per fake type and size,
- **leakage** — how much fake data slipped through as "trusted" (the key number),
- **false alarms** on clean data,
- **detection speed** (latency).

Don't have a detector yet? Use the built-in reference one to see it end-to-end:

```bash
uv run python -m eval.run_baselines --suite dev          # reference detector's alarms
uv run python -m eval.run --suite dev --alarms alarms/baseline_combined/dev
```

---

## 6. Extra tools

```bash
uv run python -m eval.report --suite showcase            # figures -> docs/figures/
uv run python -m eval.bench_crypto                        # signature cost (security overhead)
uv run python -m eval.escape --detector eval.run_baselines:detect   # evasion: smallest fake missed
```

---

## Where things live

- `data_prep/` — get & clean the data   ·   `attacks/` — make the fakes + answer key
- `eval/` — score detectors + yardstick detectors   ·   `tests/` — 238 automated checks
- `docs/` — the report material (`attack_catalogue.md`, `evaluation_protocol.md`, `results.md`, `figures/`)
- `GUIDE.md` — plain-language overview

## Dataset

WegenerNet climate station network Level 2 data v8.0 (2007–2024), Feldbach Region, 5-min, UTC.
Fuchsberger J., G. Kirchengast, C. Bichler (2025), doi:10.25364/WEGC/WPS8.0-2025.1, CC BY 4.0.
