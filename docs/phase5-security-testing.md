> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](requirements.md).

# Phase 5: attack experiments, replay and Layer 3 handoff

This release implements the independently executable portions of steps 24–32.
It does not contain the detector, which came later. All demonstration data are
explicitly synthetic. Experiment signing proves which local experiment generator
signed the input; it cannot establish the authenticity of an original dataset.

## Interfaces and trust

`WindowPipeline` in `core/pipeline.py` passes a validated `FeatureWindow` directly
to a Python callable. The demonstration callable only records receipt. It does
not score anomalies. Existing `ProvenancePipeline` sends every provenance verdict
to the required L4 sink, including rejected and malformed records. Window building
uses all verifier events, retaining nulls and rejected evidence; it does not use
the older eligible-only optional L2 callback.

L3 consumers must handle unavailable feature groups, null feature values,
quality flags, and raw provenance statuses. They must deduplicate `window_id` if
callbacks are retried. Exceptions propagate. `WindowPipeline.deliver(window)`
allows explicit redelivery of a retained window. L3 delivery is synchronous and
not durable; a failed replay can be reproduced in a fresh evaluation registry.
L4 delivery retains the existing durable, at-least-once event journal. L3 scores
and L4 decisions have no invented contract in this release.

## Eight pre-signing false-data families

`attacks.false_data.inject(measurements, AttackConfig(...))` returns immutable
transformed measurements, the full configuration and evaluation-only labels.
Input is bounded to 10,000 samples. Target sensor IDs, per-sensor half-open attack
indices, seed and numerical parameters are explicit. Targeted observations must
already be Celsius temperatures; conversion is an explicit upstream decision.
Untargeted observations and all non-value fields remain unchanged. Nulls remain
null; an active null is not counted as an applied perturbation. Inputs must be
chronological per sensor. Non-finite parameters/results fail visibly.

| Family | Definition |
| --- | --- |
| Bias / offset | Add `offset_c` during the attack interval |
| Slow drift | Add slope times elapsed seconds since the first non-null active sample |
| Spike / pulse | Add offset for `pulse_width` samples every `pulse_every` samples |
| Noise / flatten | Anchor + flatten factor × deviation from anchor + seeded Gaussian noise; zero noise gives pure flattening |
| Stuck-at / freeze | Hold the first non-null active value |
| Synthetic mimicry | Repeat the explicitly supplied benign template |
| Pattern masking | Blend original values with the explicit template using `mask_weight` |
| Colluding neighbors | Apply a common offset to two or more explicitly selected sensors |

These definitions are configurable prototype scenarios, not claims of successful
model evasion. Collusion sensor selection must be coordinated with the configured
neighbor graph; the generator does not invent or attest geographic relationships.
Templates must come from an allowed training/calibration source when real evaluation
begins. The current synthetic templates are not trained models or official samples.
No held-out test data should be used to choose a template or tune an attack.

Each label records source identity, sensor, local sample index, original/injected
value, family, application and actual-change flags. An applied transformation may
leave a value unchanged (e.g. the first freeze/drift sample). Labels stay outside
wire envelopes and FeatureWindow. `Recording.source_records` joins the recording
position to source rows; decode the corresponding payload to obtain its signed
identity for evaluation. Retain the original dataset and configuration separately.

`sign_experiment` creates fresh ephemeral keys and isolated signer databases in a
new directory. It emits exact CBOR bytes and public enrollment metadata; it never
exports private keys. Fresh signing runs have different keys. Replaying the same
recording, policies and enrollment is deterministic.

## Wire scenarios

`tamper_temperature` modifies a signed value without updating its signature.
`break_chain` uses a matching compromised experiment key to sign a deliberately
incorrect predecessor. The demonstration also covers unknown keys, duplicate
messages, deletion and reordering. Deletion/reordering yields `gap` when a
predecessor is absent; it is not automatically proof of malicious tampering.
An invalid signature does not advance the trusted sequence. A valid signature
with the wrong predecessor produces `chain_anomaly`. Correct predecessors can
restore continuity within the normal freshness policy.

## Replay API

```python
from adapters.replay import ReplayAdapter
from core.provenance.keyregistry import KeyRegistry

registry = KeyRegistry.create(new_registry_path)
recording.enroll(registry)
received_windows = []
audit_events = []
result = ReplayAdapter(
    registry,
    layer4=lambda event_id, verdict: audit_events.append((event_id, verdict)),
    layer3=received_windows.append,
).run(recording.records, as_of=explicit_aware_utc_cutoff)
```

`ReplayRecord(payload=..., received_at=..., topic=...)` represents a local replay
input, not a dataset schema. Exact recorded wire bytes are verified with the
production verifier. The arrival clock is injected explicitly, not disabled;
freshness checks remain active. Arrivals must be nondecreasing and no later than
`as_of`. Payloads may intentionally be reordered while arrival times remain
ordered. Topics are checked when supplied. Signed timestamps are never shifted.

Use an exclusively owned fresh registry, explicitly enrolled before replay.
Replay rejects an already-used registry and never resets live sequence state.
Do not share this registry with a live receiver or another replay. The adapter
is single-use after verification starts, including failures. A fresh replay can
reproduce an interrupted evaluation. It does not resume mid-chain automatically.
Closed windows are built by the production ProcessingEngine; unassigned and
excluded evidence is retained in ReplayResult/WindowResult. No wall-clock sleeps,
MQTT broker or detector is required for offline execution. Bounds remain those
of the processing policy (10,000 events by default; at most 128 closed windows).

## Dataset boundary

`adapters/dataset.py` defines a typed `DatasetAdapter` protocol and `Measurement`
boundary with source ID, source record ID, original value/unit/time/location.
No CSV column names, timezone assumptions or missing-value codes are hard-coded. A
concrete adapter maps a dataset's documented columns, license, timezone/unit rules
and evaluation split (the WegenerNet adapter is `adapters/wegenernet.py`). The
existing `ReferenceObservation` boundary remains the external-reference interface.

## Commands

From the directory containing `pyproject.toml`:

```text
python -m scripts.demo_attacks
python attack_test.py
python -m pytest -q tests
python -m pytest -q tests --run-broker --broker-executable PATH_TO_MOSQUITTO
```

The demo runs eight signed families and six wire-security scenarios in temporary
directories. It reports actual statuses, changed counts, normalized readings and
L3 stub receipt. It explicitly reports `detection_evaluated: false`.

## Requirement status

| Step | Implemented/tested | Pending |
| --- | --- | --- |
| 24 | Direct FeatureWindow handoff and validated receiver stub; callback failure handling | Team acceptance and real L3 integration |
| 25 | Typed dataset boundary with source-row traceability | Dataset mapping and integration (done later: WegenerNet) |
| 26–29 | Post-sign tamper, unknown sensor, replay, deletion/reordering and signed chain break | Standard scenario integration (done later: `attacks/`) |
| 30 | All eight signed attack families, L1 validation, L2 extraction and stub handoff | Real anomaly detection and evaluation |
| 31 | Deterministic bounded replay of captured bytes using production L1b/L2 | Real historical dataset recordings |
| 32 | Unit/integration tests, including real mTLS attack delivery | Native Windows Phase 5 execution |

Phases 6 and 7 have not been started. No detection rate, false-alarm rate,
performance benchmark or challenge-submission readiness is claimed.
