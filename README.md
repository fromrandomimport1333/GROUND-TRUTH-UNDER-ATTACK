# Ground Truth Under Attack — Track 2: False-Data Injection

A defensive prototype for the **in-situ sensing segment** of the Earth-observation chain. A digital
signature proves *who* sent a weather reading, not that it is *true*. This system checks both: every
reading is signed and verified (provenance), cleaned and aligned (signal processing), scored by a
trained detector (false-data detection), and turned into a trust decision with a tamper-evident audit
trail (response). Everything runs on a laptop CPU with public data and a software trust anchor.

**Phase 1 report:** [docs/report.pdf](docs/report.pdf), IEEE conference format (LaTeX source in `docs/report/`).

## The four layers

```
sensor ──signed record──▶ L1 provenance ──verdict──▶ L2 processing ──features──▶ L3 detector ──alerts──▶ L4 trust & response
         (Ed25519, MQTT/TLS)  core/provenance         core/processing           core/detection          core/response + dashboard
                                    └──────────────────────── every verdict, including rejections ─────────────▲
```

| Layer | What it does | Code | Documentation |
|---|---|---|---|
| 1. Provenance | Ed25519 signature, sequence number and hash chain per sensor; mutual-TLS MQTT; key registry with rotation and revocation | `core/provenance/`, `adapters/` | [layer1-2.md](docs/layer1-2.md), [phase2-security.md](docs/phase2-security.md), [phase3-security.md](docs/phase3-security.md) |
| 2. Signal processing | Unit and physical validation, UTC window alignment, temporal, spectral and spatial features | `core/processing/` | [phase4-processing.md](docs/phase4-processing.md), [contracts.md](docs/contracts.md) |
| 3. Detection | 37 causal features per reading, one Extra Trees model per variable, calibrated p-values, frozen alert rule | `core/detection/` | [core/docs/README.md](core/docs/README.md) |
| 4. Trust & response | Trusted / suspicious / quarantined state, imputation from trusted neighbours, hash-chained audit log, analyst dashboard | `core/response/`, `dashboard/` | [layer4.md](docs/layer4.md), [layer4-thresholds.md](docs/layer4-thresholds.md) |

## Headline results

| | Result | Source |
|---|---|---|
| Attacks detected (2024, two seeds, 12 families) | 79–82 % of attack events; median 60–90 min to first alert | [core/docs/README.md § 5](core/docs/README.md#5-results) |
| Precision / F1 | 57–60 % / 0.41–0.44, about 1.9× the rule ensemble on the same signals | same |
| False alarms on clean 2024 data | 0.013 episodes per stream-day (budget 0.05) | same |
| Attack families never seen in training | 81–100 % of attacks detected (pattern, mimicry, multivariate; two seeds) | same |
| In-transit and stored-record tampering | caught deterministically by Layer 1 (bad signature, replay, gap, unknown sensor) | [attack_catalogue.md](docs/attack_catalogue.md) |
| Detector latency, live | ~0.2 s per 30-minute tick for all 486 streams (median, 2-core CPU) | [reports/detector-overhead.json](reports/detector-overhead.json) |
| Detector memory | ~0.85 GB peak RSS; model artifacts 17.6 MB | same |
| Security overhead | 104 bytes per record; ~11,000 verifications/s on one core | [results.md](docs/results.md#efficiency--security-overhead) |

## Quick start

Python 3.13 or 3.14, no GPU, no paid service. Full instructions and how to read every output:
**[docs/jury-guide.md](docs/jury-guide.md)**.

```bash
python3 -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install --require-hashes -r requirements.txt
python -m pip install -r requirements-detector.txt
python -m pip install -r requirements-dashboard.txt -r requirements-wegenernet.txt

python -m pytest -q tests core/tests                      # 745 passed, 17 skipped (broker tests), no data needed
python -m eval.detector_overhead                          # detector latency and memory, no data needed
python -m scripts.demo_attacks                            # Layer 1 attack demonstration, no data needed
python -m scripts.demo_layer4 && python -m dashboard.app --db local/layer4-demo.sqlite3   # dashboard demo
```

With the WegenerNet data in `data/` (see the jury guide):

```bash
python -m core.detection.pipeline --stage evaluate        # reproduce the 2024 results with the shipped model
python -m scripts.try_detector --family drift --amplitude 4 --duration 48   # plant your own attack
```

## Data

WegenerNet Level 2 v8.0 (release 2025.1), Feldbach region, Austria, Wegener Center, University of Graz.
DOI [10.25364/WEGC/WPS8.0-2025.1](https://doi.org/10.25364/WEGC/WPS8.0-2025.1), CC BY 4.0. The detector uses
the half-hourly 100 m grid (2023–2024); the station attack suite uses the 5-minute station network.

## Repository map

| Path | Content |
|---|---|
| `core/` | The four layers, their contracts and Layer 3–4 tests |
| `artifacts/` | The trained, hash-frozen Layer 3 model |
| `attacks/`, `data_prep/` | Labelled attack scenarios on station data, data preparation |
| `eval/` | Scoring, baselines, evasion sweep, performance and overhead measurements |
| `dashboard/` | Layer 4 analyst dashboard (FastAPI, server-rendered HTML) |
| `scripts/` | Command-line tools and demonstrations |
| `config/` | Detector, processing and transport settings |
| `docs/` | Report, guides and design documents; [project-structure.md](docs/project-structure.md) describes every folder |
| `reports/` | Measurement outputs; `reports/README.md` says which are current |
| `tests/` | Layer 1–2 and attack-suite tests |

Other guides: [evaluation suite](docs/evaluation-suite.md), [attack catalogue](docs/attack_catalogue.md),
[evaluation protocol](docs/evaluation_protocol.md), [threat model](docs/threat-model.md),
[requirement tracking](docs/requirements.md), [plain-language tour](GUIDE.md).
