# Requirement status against the challenge

Status of every requirement of the official challenge brief (Track 2: False-Data Injection) at the Phase 1
submission. "Evidence" points to the code, test or result that supports the claim.

## Six mandatory deliverables

| Deliverable | Status | Evidence |
| --- | --- | --- |
| 1. Threat model & segment analysis | Done | [threat-model.md](threat-model.md), [core/docs/data_and_threat_model.md](../core/docs/data_and_threat_model.md), [attack_catalogue.md](attack_catalogue.md) |
| 2. Platform / architecture / trust anchor | Done (software trust anchor) | Ed25519 + hash chain + key registry in `core/provenance/`; [layer1-2.md](layer1-2.md), [phase2-security.md](phase2-security.md), [phase3-security.md](phase3-security.md) |
| 3. Signal processing | Done | `core/processing/` (validation, alignment, rolling/spectral/spatial features); 37 causal detector features in `core/detection/features.py` |
| 4. AI or statistical detection | Done | Extra Trees per variable, calibrated p-values, frozen alert rule: [core/docs/README.md](../core/docs/README.md) |
| 5. Adversarial validation & response | Done | 12 attack families incl. 3 unseen; station suite with held-out stations and an evasion sweep; Layer 4 trust/quarantine/imputation: [layer4.md](layer4.md) |
| 6. Documentation with reference datasets | Done | [report.pdf](report.pdf), [jury-guide.md](jury-guide.md); WegenerNet L2 v8.0, CC BY 4.0 |

## Challenge goals

| Goal | Status | Evidence |
| --- | --- | --- |
| Use authentic public data | Done | WegenerNet Level 2 v8.0 (DOI 10.25364/WEGC/WPS8.0-2025.1), gridded half-hourly and 5-minute stations |
| Apply the common standard scenario | Partly | Versioned, seeded standard scenario on real WegenerNet data (`attacks/`, [evaluation_protocol.md](evaluation_protocol.md)). The trained detector is evaluated on its own chronological held-out protocol; grading it on the station suite needs a station-data retrain (Phase 2) |
| Detect or prevent the attack | Done | Layer 1 prevents in-transit/stored tampering; Layer 3 detects valid-signature false data; Layer 4 withholds or imputes |
| Report detection quality | Done | Precision, recall, F1, event detection, delay: [core/docs/README.md § 5](../core/docs/README.md#5-results) |
| Report false alarms | Done | 0.013 false-alert episodes per stream-day on clean 2024 |
| Report latency | Done | Detector live tick median 202 ms for 486 streams: [results.md](results.md#detector-latency-and-memory) |
| Report memory | Done | Detector peak RSS 852 MB; Layer 1–2 in `reports/phase6-benchmark-linux.json` |
| Report security overhead | Done | 104 B/record, ~11,300 verifications/s: [results.md](results.md#efficiency--security-overhead) |
| No purchase; local laptop execution | Done | CPU only, no GPU, cloud service or paid API |

## Phase 1 rules

| Rule | Status |
| --- | --- |
| Exactly one track | Track 2 |
| Team of at most 5 | Team matter |
| Anonymous submission (no names, unit names, logos, identifying metadata) | Authored files checked; submit without the development Git history ([anonymity.md](anonymity.md)) |

## Known limitations carried into Phase 2

1. Detector trained on the half-hourly grid; retrain and grade it on the station suite (`dev`, `heldout`, `crosssource`).
2. Coordinated whole-network bias needs an independent reference (ERA5-Land or satellite); the adapter exists
   (`data_prep/reference.py`) and requires a Copernicus key.
3. Weak families: single-step spikes, rain scaling, wide patches ([core/docs/README.md § 9](../core/docs/README.md#9-scope-and-limitations)).
4. Software trust anchor only: a compromised verifier host or stolen keys are out of scope.
5. Layer 4 trust state is in memory; the audit log persists.
