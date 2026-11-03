# Threat model

The defended segment is in-situ sensor ingestion into an Earth-observation
analysis pipeline (Track 2: false-data injection). The Layer 3 attacker model,
attack families and leakage controls are detailed in
[core/docs/data_and_threat_model.md](../core/docs/data_and_threat_model.md); the full
attack catalogue with expected catching layer is in [attack_catalogue.md](attack_catalogue.md).

Protected assets are measurement integrity, sensor identity, freshness,
processing evidence, detector inputs and security decisions. Trusted components
are the verifier host, provisioned registry, local policy/state, processing code
and configured trust roots. A software trust anchor is the initial target.

| Attacker capability | Control (implemented) | Residual limitation |
| --- | --- | --- |
| Modify a measurement in transit | Ed25519 integrity verification and TLS | Endpoint compromise remains possible |
| Publish under an unknown sensor/key | Authorized key registry and broker ACLs | Registry provisioning must be protected |
| Replay an authentic record | Per-stream sequence/time checks | Authorized resets and restarts need explicit policy |
| Delete or reorder messages | Hash-chain and sequence findings | Network loss and malicious deletion cannot always be distinguished |
| Control a legitimate signing sensor | Temporal/spatial/reference features and L3 detection | Valid signatures do not imply accurate observations |
| Coordinate neighboring compromised sensors | External references and adversarial evaluation | Shared-source compromise can weaken consensus |
| Send malformed or oversized payloads | Bounded decoder and schema validation | Resource limits must precede parsing |
| Poison replay state with forged messages | Authenticate before atomic state updates | Persistence and concurrency require verification |
| Introduce evaluation labels into detection | Separate evaluation metadata and audit features | Schema exclusion alone cannot stop all leakage |

The prototype does not promise protection from a compromised verifier host,
unlimited denial-of-service, theft of all trust anchors, perfect detection of
physically plausible falsifications, or a provable distinction between every
network fault and attack. These limitations guide evaluation rather than
excusing missing controls.

All controls in the table are implemented and tested: Layer 1 controls in
`core/provenance/` (tests `tests/test_phase2_*`, `tests/test_phase3_*`,
`tests/test_security_boundaries.py`), Layer 3 detection in `core/detection/`
(`core/tests/test_detector.py`, including the leakage tests) and the Layer 4
response in `core/response/`. Status per requirement: [requirements.md](requirements.md).
