> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](requirements.md).

# Architecture decisions

The updated 40-step plan is the implementation baseline. Its modular structure
and advanced capabilities supersede the older architecture's simplified layout.

| Decision | Reason and consequence |
| --- | --- |
| L1a signs, L1b verifies, L2 processes | Keeps authentication independent from physical plausibility |
| Core logic independent of MQTT | Live and deterministic replay can share verification and processing |
| Direct Python contracts to L3/L4 | Avoids an unnecessary internal broker round trip; optional exports can be adapters |
| Canonical CBOR signing | Implemented in Phase 2 with fixed vectors; JSON remains diagnostic |
| Preserve original signed objects | Cleaning cannot erase attack evidence or change signed fields |
| Separate key and stream IDs | Supports explicit rotation/restart policy without changing sensor identity |
| Separate signature verdict, findings and eligibility | Authentication success does not establish data truth |
| Immutable nested models | Prevents accidental mutation through collections and derived records |
| Generic quantity/value fields | Dataset columns remain adapter concerns; temperature is the first simulator use case |
| Explicit feature availability | Missing geography or references cannot appear as fabricated evidence |
| Explicit event time and availability time | Supports causal replay and prevents accidental future-data evaluation advantages |
| Locks include platform markers and hashes | Enables reproducible Python dependency installation across supported targets |

The key registry must authorize streams and rotations. An attacker-selected new
stream ID must not reset replay protection. Authentication failure must never
advance sequence/hash-chain state. Phase 3 implements atomic state updates as detailed below, with
bounded persistence, clock injection, crash recovery, duplicate delivery,
reordering, gaps and resynchronization.

All eight advanced signed attack families remain planned: bias/offset, slow
drift, spike/pulse, noise/flatten, stuck-at/freeze, synthetic mimicry, pattern
masking and colluding neighbors. Spectral features remain conditional on
sampling suitability; spatial/reference features remain conditional on real
metadata and source availability. The conditional feature implementations are now present in Phase 4; all eight
attack families remain planned for Phase 5.

No detector, response, benchmark, long-duration stability result or challenge
score is claimed. TLS, ACLs, secure key files, bounded CBOR and persistent
provenance were implemented and tested in Phases 2/3.

## Phase 3 decisions

Keep the v1 wire and internal contracts unchanged. Use a separate receiver SQLite
store with explicit key/stream enrollment, immutable replay history, atomic head
and verdict commits, irreversible revocation and explicit rotation. Do not advance
a stream on gaps, invalid signatures, expired messages or wrong hashes. Missing
predecessors may restore continuity; automatic reanchoring is deferred.

Provide the shared pipeline seam now because step 16 requires all provenance
statuses to reach L4. L2/L4 callbacks are injectable; stdout is only the demo sink.
Consumers must deduplicate event IDs for at-least-once delivery. Actual signal
processing, detector and response logic remain their planned phases. Detailed
policy, limitations and recovery instructions are in `phase3-security.md`.

## Phase 4 decisions

Keep SignedReading/VerifiedReading/FeatureWindow schemas unchanged. Use a pure,
bounded ProcessingEngine with explicit UTC interval and availability cutoff.
Retain full raw provenance; never coerce strings into signed numbers, fabricate
samples, shift signed timestamps or silently discard conflicting evidence.
Snapshot IDs include policy and contents. Rolling statistics stop at missing or
ineligible samples and stream discontinuities. Spectral calculations require
regular sampling; absent neighbors/references yield explicit unavailable groups.

Read the complete trusted verifier journal for Phase 4 snapshots because the
optional Phase 3 analytical callback omits null and rejected observations.
WindowResult carries excluded evidence, selected reference lineage and immutable
policy alongside the unchanged FeatureWindow. Production streaming, dataset
adapters and the actual L3/L4 consumers remain later integration work.

## Phase 5: isolated replay and detector handoff

The production verifier and ProcessingEngine are reused for offline evaluation.
An explicit historical arrival clock preserves freshness semantics without
altering signed timestamps. Replay uses a fresh, exclusively owned registry;
production sequence state is never rewound. L4 receives every verdict through
the existing durable journal, while L2 receives the complete replay event batch
so missing/rejected evidence remains visible. WindowPipeline passes validated
FeatureWindow objects directly to a Python L3 callable. Callback exceptions
propagate; L3 delivery is synchronous and consumers must deduplicate retries.

Attack transformations occur before normal signing. Post-sign wire mutations
are a separate experiment. Attack labels and original baseline values remain
outside the detector input contract to prevent evaluation label leakage.
Fresh experiment keys are random; reproducibility is defined over the saved
recording, enrollment, policies and references. Dataset schemas and detector
outputs remain undefined until actual team inputs are supplied. The shared
SignedReading, VerifiedReading and FeatureWindow schemas have not changed.

## Phase 6: measurements and conservative recovery

Transport retry wrappers preserve the production mTLS requirement and resend
identical signed bytes. Receiver reconnects keep the original registry and
head state. Clean-session transport still needs explicit recovery for records
missed during receiver outages. An optional separately provisioned bounded
archive retains exact signed bytes before sender publication. Its full condition
stops sending rather than dropping evidence. Original timestamps and replay
policy remain enforced during explicit sequence-range retransmission.

Performance controls are confined to evaluation code and owned temporary
loopback brokers. Plaintext is not a production setting. Reports distinguish
primitive crypto, durable filesystem work, broker ACK and subscriber callback
latency. Resource reports identify Python-only CPU/RSS and separately traced
Python allocations. The existing data contracts and Phase 5 attacks are preserved.
Metrics accept explicit evaluation labels/predictions with exact ID matching;
no real L3 results are synthesized. Final four-layer integration remains Phase 7.
