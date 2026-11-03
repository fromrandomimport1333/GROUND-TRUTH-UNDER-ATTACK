# Project structure

What every folder is for. The system has four layers that run in one Python process:

```
sensor ──signed record──▶ L1 provenance ──verdict──▶ L2 processing ──features──▶ L3 detector ──alerts──▶ L4 trust & response
         (Ed25519, MQTT/TLS)  core/provenance         core/processing           core/detection          core/response
                                    └──────────────────────── verdicts (all statuses) ─────────────────────────▲
```

To test the model as a jury member, read **[jury-guide.md](jury-guide.md)**.

## Top level

| Path | Purpose |
|---|---|
| `README.md` | Entry point: overview, headline results, quick start, links to every layer |
| `GUIDE.md` | Plain-language tour of the attack-scenario and evaluation suite |
| `pyproject.toml` | Package definition and pytest settings (`tests/` is the default test folder) |
| `requirements.txt` | Hash-pinned core dependencies (Layers 1–2, tests): `pip install --require-hashes -r requirements.txt` |
| `requirements-detector.txt` | Layer 3 detector dependencies (scikit-learn, xarray, …) and matplotlib |
| `requirements-dashboard.txt` | Layer 4 dashboard (FastAPI, uvicorn; httpx for its tests) |
| `requirements-wegenernet.txt` | NetCDF and projection libraries for the WegenerNet Layer 1–2 export |
| `requirements.in` | Unpinned source list behind `requirements.txt` |
| `uv.lock` | Lock file used by the attack-scenario suite's `uv` workflow |
| `sensor.py` | Multi-sensor Layer 1a simulator: signs readings and publishes them over MQTT/TLS |
| `generate_keys.py` | Creates encrypted Ed25519 sensor keys in a new folder (never overwrites) |
| `benchmark.py` | Measures signing, verification and processing cost (optionally over a real broker) |
| `stability.py` | Soak test: three sensors over mutual TLS with injected failures and recovery |
| `attack_test.py` | Entry point for the controlled Layer 1 security demonstration |

## `core/` — the four layers

| Path | Purpose |
|---|---|
| `core/contracts.py` | Typed data contracts passed between layers: `SignedReading`, `VerifiedReading`, `ProcessedReading`, `FeatureWindow`, `TrustDecision`, `VariableEvidence` |
| `core/pipeline.py` | Wiring: `ProvenancePipeline` sends every Layer 1 verdict to Layer 4 (and eligible ones to Layer 2); `WindowPipeline` sends FeatureWindows to Layer 3 |
| `core/local_config.py`, `core/secure_files.py` | Local provisioning manifest; files created with restrictive permissions |
| **`core/provenance/`** | **Layer 1 — provenance.** `signer.py` signs each reading (Ed25519, sequence number, hash of the previous record); `verifier.py` checks signature, sequence, time window and hash chain and returns a status (`valid`, `bad_signature`, `replay`, `gap`, …); `keyregistry.py` holds trusted sensor keys (rotation, revocation); `codec.py` is the canonical CBOR wire format; `state.py`, `replay_protection.py`, `recovery.py`, `keys.py`, `pki.py` handle durable sequence state, time policy, retained bytes, key storage and the local TLS CA |
| **`core/processing/`** | **Layer 2 — signal processing.** `validator.py` checks units and physical meaning after verification; `engine.py` and `alignment.py` build fixed UTC windows; `features.py` and `hourly.py` compute rolling, spatial (neighbour) and quality features into a `FeatureWindow`; `references.py` matches external reference data when available; `journal.py` reads the verifier's evidence; `policy.py` holds the processing settings |
| **`core/detection/`** | **Layer 3 — the false-data detector.** `data.py` turns the NetCDF files into a compact cache and 14-day episodes; `features.py` computes 37 causal features per reading; `model.py` holds the Extra Trees models, calibration, alert rule and the `Detector` class; `attacks.py` generates the synthetic attacks used for training and testing; `metrics.py` computes precision, recall and false-alarm episodes; `pipeline.py` runs everything (`python -m core.detection.pipeline`); `adapter.py` converts Layer 2 output into detector input |
| `core/docs/` | Layer 3 documentation: `README.md` (full detector description), `detector_walkthrough.ipynb` (step-by-step notebook), `data_and_threat_model.md`, `layer3_specification.docx`, `figures/` (with `make_figures.py`) |
| `core/results/` | The reference results of the detector (2023 model selection, 2024 evaluation) cited in the documentation |
| **`core/response/`** | **Layer 4 — trust and response.** `trust.py` turns Layer 1 verdicts and Layer 3 alerts into a sensor state (trusted / suspicious / quarantined), actions (flag, quarantine, impute, notify) and released values; `auditlog.py` is the append-only, hash-chained SQLite audit log |
| `core/tests/` | Tests for Layers 3 and 4 and the dashboard (`python -m pytest core/tests`) |
| `artifacts/` (repository root) | **The trained Layer 3 model**, shipped in git: feature extractor, Extra Trees models, calibration and the frozen alert `policy.json` (with the SHA-256 of each file). `python -m core.detection.pipeline --force` retrains it |

## Other code

| Path | Purpose |
|---|---|
| `adapters/` | Input/output boundaries: `live_mqtt.py` and `mqtt_transport.py` (mutual-TLS MQTT), `replay.py` (offline replay of recorded wire bytes through the real code), `wegenernet.py` (reads WegenerNet NetCDF grids for Layers 1–2), `feature_export.py` (writes FeatureWindows to CSV/JSONL), `dataset.py`, `reconnect.py` |
| `attacks/` | Attack scenarios. **Station suite** (from the attack-scenarios branch): `schema.py`, `families.py`, `grid.py`, `placement.py`, `injector.py`, `build_suites.py` build labelled attacks on 5-minute station data; `records.py`, `build_records.py` build record-level (in-transit) attacks; `adaptive.py` measures escape magnitude. **Layer 1 experiments**: `false_data.py`, `wire.py`, `security_demo.py`, `scenario.py`, `offline_state.py`. `scenarios/*.yaml` hold the suite grids |
| `data_prep/` | Station-data preparation for the attack suite: `download.py` (WegenerNet 5-minute station zips), `build.py` (clean tables), `stations.py` (locations, neighbours), `wegenernet.py` (CSV reader), `reference.py` (ERA5 reference for the cross-source check) |
| `eval/` | Evaluation tools: `scoring.py`, `run.py`, `run_baselines.py`, `baselines.py` (score detectors on the station suites); `escape.py` (evasion boundary); `report.py` (figures); `detector_overhead.py` (Layer 3 latency and memory), `overhead.py`, `performance.py`, `bench_crypto.py`, `transport_benchmark.py`, `local_broker.py` (cost and transport measurements); `metrics.py`; `layer4_threshold_sim.py` (simulation behind the Layer 4 thresholds) |
| `dashboard/` | Layer 4 analyst dashboard: `app.py` (FastAPI, server-rendered HTML, JSON API) and `static/style.css` |
| `scripts/` | Command-line tools. **Jury-facing:** `try_detector.py` (plant an attack, see the detector's answer), `demo_layer4.py` (demo data for the dashboard). **Layers 1–2:** `setup_local.py`, `manage_registry.py`, `manage_recovery.py`, `receive_mqtt.py`, `observe_mqtt.py`, `demo_signing.py`, `demo_processing.py`, `demo_attacks.py`, `build_feature_window.py`, `export_schemas.py`. **WegenerNet export:** `export_wegenernet*.py`, `validate_wegenernet_export.py`, `inspect_wegenernet_flags.py`, `compact_audit_lineage.py`. `check_environment.py` checks dependencies |
| `tests/` | Tests for Layers 1–2 and the attack suite (`python -m pytest tests`); broker tests need `--run-broker` and a local Mosquitto |

## Configuration — `config/`

| File | Purpose |
|---|---|
| `detector.yaml` | Every Layer 3 setting: data location and splits, features, attack families, model, alert policy, evaluation seeds |
| `cells.txt` | The 30 grid cells exported by the Layer 1–2 WegenerNet workflow |
| `development.json` | Design defaults for the MQTT/TLS transport (from Phase 1; not read by the current code) |
| `processing.example.json` | Example Layer 2 processing policy (window size, rolling samples, spatial and reference limits) |

## Documentation — `docs/`

| File(s) | Content |
|---|---|
| `report.pdf`, `report/` | The Phase 1 report (IEEE conference format) and its LaTeX source: `cd docs/report && latexmk -pdf report.tex` |
| `jury-guide.md` | How to install, test and read the output of the model |
| `layer1-2.md` | Layers 1–2 operator detail: signing demo, MQTT/TLS demo, delivery semantics, source layout |
| `project-structure.md` | This file |
| `layer4.md`, `layer4-thresholds.md` | Layer 4 design, dashboard, thresholds and their justification |
| `contracts.md`, `schemas/*.json` | Inter-layer contracts (prose and JSON Schema) |
| `architecture-decisions.md`, `threat-model.md`, `requirements.md`, `anonymity.md` | Design decisions, threat model, requirement status against the challenge, submission anonymity |
| `evaluation-suite.md`, `attack_catalogue.md`, `evaluation_protocol.md`, `results.md`, `manifests/`, `figures/` | The station attack suite: how to run it, the attack families, the protocol, recorded baseline results |
| `half-hourly-integration.md`, `hourly-integration.md`, `layer3-half-hourly-handoff.md` | WegenerNet export from Layers 1–2 and the hand-off to Layer 3 |
| `phase2-security.md` … `phase6-performance-reliability.md` | Design notes for Layers 1–2 per development phase |
| `windows-*.md` | Windows (PowerShell) instructions per development phase |
| `standard-scenario-design.md` | Design specification of the station attack suite (families, grids, splits) |

## Evidence — `reports/`

Recorded verification runs per development phase (test output, benchmark and stability JSON). These are
historical evidence and are not regenerated automatically.

## Not in git (created locally)

| Path | Created by | Content |
|---|---|---|
| `.venv/` | `python3 -m venv .venv` | Python environment |
| `data/` | you (download) and the detector | WegenerNet NetCDF folders and `data/cache/` (the detector's extracted cache) |
| `outputs/` | the detector pipeline (`evaluate`) | `metrics.json`, `variables.csv`, `families.csv`, `threshold_search.csv`, `RESULTS.md` |
| `local/` | demo and helper scripts | Demo audit log, logs, CSV output of `try_detector` |
