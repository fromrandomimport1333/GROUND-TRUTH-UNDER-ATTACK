# Project Guide

**What this project is:** a test rig for Track 2 (False-Data Injection). It takes real,
clean weather-station data, fakes parts of it in many ways, and scores how well a detector
catches the fakes.


---

## The folders

| Folder | Plain English | Key files |
|---|---|---|
| `data_prep/` | **Gets and cleans the real data.** Downloads the WegenerNet station files and turns them into tidy tables (one per measurement). Also computes each station's "normal" vs its neighbours. | `download.py`, `build.py`, `stations.py` |
| `attacks/` | **Makes the exam.** Defines the 15 fake types, places them on the 2024 calendar, stamps them into clean data, and writes the answer key. | `families.py` (the fakes), `injector.py` (stamps them in), `placement.py` (where/when), `build_suites.py` (assembles the exams) |
| `attacks/scenarios/` | **The exam recipes** — small settings files saying which fakes, how big, how long. | `grid_dev.yaml`, `grid_heldout.yaml`, `showcase.yaml` |
| `eval/` | **The grader + yardstick detectors.** Scores a detector against the answer key (catch rate, misses, false alarms, speed), plus simple reference detectors and the efficiency/figures tools. | `scoring.py`, `run.py` (grade), `baselines.py` (reference detectors), `report.py` (figures) |
| `tests/` | **Proof it all works.** 235 automated checks, one set per piece. | `test_*.py` |
| `docs/` | **The write-up.** The attack catalogue, the evaluation method, the results, and the figures for the report. | `attack_catalogue.md`, `evaluation_protocol.md`, `results.md`, `figures/` |
| `data/` | **The actual data and built exams** (not in git — too big; rebuilt by commands). | `clean/`, `suites/` |

### The exams that get built (in `data/suites/`)

| Name | What it's for |
|---|---|
| `smoke` | 28 quick fakes — for fast checking while developing |
| `dev` | 342 fakes — the main practice exam teammates tune the detector on |
| `heldout` | 178 fakes with **different** stations/sizes/shapes — the "secret" exam, like the judges' hidden test |
| `clean2024` | the whole year with **no** fakes — to measure false alarms |
| `showcase` | one of each fake type — for the demo and the figures |
| `crosssource` | the "whole-network" fake (the one that needs an outside reference) |

---

## How to test it

All commands run from the project root. The tool is `uv`.

### 1. Prove the code works (no data needed) — ~10 seconds
```bash
uv run pytest -q
```
Expect: `235 passed`. This runs every automated check. **This alone proves the whole thing works.**

### 2. See the data is real and cleaned
```bash
uv run python -I -c "import pandas as pd; d=pd.read_parquet('data/clean/air_temperature.parquet'); print(d.shape, 'rows x stations'); print(d.iloc[:3, :4])"
```
Expect: a table of real temperatures, 210528 rows × 154 stations.

### 3. Rebuild the exams and check they're reproducible — ~30 seconds
```bash
uv run python -m attacks.build_suites
```
Expect: `smoke: 28 ... dev: 342 ... heldout: 178 ... showcase: 15`. Each exam folder gets a
`manifest.json` with fingerprints; rebuilding gives identical files (that's the reproducibility
points).

### 4. Run a reference detector and grade it — ~1 minute
```bash
uv run python -m eval.run_baselines --suite dev
uv run python -m eval.run --suite dev --alarms alarms/baseline_combined/dev
```
Expect: a `results/.../summary.json` and `tables.md` with catch rates, false alarms, and the
"fake data that reached the analyst" number. This is exactly how your teammates' detector will
be graded — they just swap in their own alarms.

### 5. Look at the pictures
```bash
xdg-open docs/figures/gallery.png
```
Expect: one small panel per fake type, clean (blue) vs faked (orange) — a visual that every
fake is working.

---

## How your teammates plug in their detector

Their detector must expose one function:
```python
detect(values: dict[str, DataFrame]) -> dict[str, DataFrame]   # boolean "alarm" tables, same shape
```
Then grading their detector is the same `eval.run` command, pointed at their alarm files. The
full method is in `docs/evaluation_protocol.md`; the fake catalogue is in `docs/attack_catalogue.md`.

## The one optional piece (ignore if confusing)

`data_prep/reference.py` + the `crosssource` exam handle a single tricky fake — faking *every*
station at once — which needs an outside weather reference (ERA5 gridded weather data, **not**
satellite images). Everything else works without it. If it's a distraction, it can be removed
and nothing breaks.
