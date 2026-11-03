# Internal data contracts

Version 1.0 of the typed contracts between the layers (`core/contracts.py`). Generated JSON schemas are
in `docs/schemas/`. Layer 3 consumes Layer 2 output through `core/detection/adapter.py`; Layer 4 consumes
Layer 1 verdicts and Layer 3 rows as described in [layer4.md](layer4.md).

## Shared rules

All models forbid extra fields, are frozen and revalidate nested instances.
Collections are immutable tuples; JSON represents them as arrays. Scalar
numeric measurements/features require finite Python floats; strings, booleans
and integers are rejected rather than silently converted. Dataset adapters must
make deliberate conversions and retain original source records when introduced.
Extremely high or low finite values remain available for anomaly detection.

IDs are bounded ASCII identifiers. Unit strings are bounded, nonempty and
exclude control characters. Unknown units remain representable in raw readings:
unit compatibility is semantic Layer 2 work in Phase 4, not envelope parsing.
Null values represent explicit missing observations, not zero or an imputation.
Location is optional; latitude/longitude validation checks coordinate ranges,
not the truth of a sensor's claimed location.

Pydantic validation is not a trust anchor. `model_construct` and unchecked
`model_copy(update=...)` must not be used on untrusted input. Use constructors
or `model_validate`/`model_validate_json`. Models do not replace input byte-size
limits or a bounded decoder. Those are required in the live adapter.

## SignedReading

| Field | Representation and meaning |
| --- | --- |
| `schema_version` | Required string `1.0` |
| `sensor_id` | Sensor identity resolved against trusted registry |
| `key_id` | Key selection; not self-authorizing |
| `stream_id` | Sequence/hash-chain namespace; restart authorization is external |
| `timestamp` | Exact UTC string `YYYY-MM-DDTHH:MM:SS.ffffffZ` |
| `sequence` | Integer from 0 through 2^63 - 1 |
| `quantity` | Quantity identifier, initially `temperature` |
| `value` | Finite float or explicit null |
| `unit` | Original unit string |
| `location` | Optional latitude/longitude model, default null |
| `prev_hash` | Exactly 32 bytes; lowercase hex in JSON |
| `signature` | Exactly 64 bytes; lowercase hex in JSON |

The timestamp string is preserved verbatim after validation; `observed_at`
provides a derived datetime. The original signed envelope must remain separate
from normalized and imputed values. Missing required fields are errors.

## Serialization implemented in Phase 2

Python/CBOR uses native byte strings for hash/signature fields. JSON diagnostic
exports use lowercase hexadecimal. The signed timestamp remains a string.
Explicit model defaults, including null location, are included.

Signing bytes are `b"CIS/reading/v1\0"` followed by canonical CBOR of
the complete SignedReading field map excluding only `signature`. The chain
digest is SHA-256 of `b"CIS/chain/v1\0"` followed by canonical CBOR of the full
signed envelope. Genesis `prev_hash` is 32 zero bytes. Every payload field,
including identity, timestamp, key, stream, location and previous hash, is covered.

These algorithms, fixed byte/signature vectors and key handling are implemented
and tested in Phase 2. The bounded decoder rejects duplicate keys, trailing
objects, semantic tags, arrays, excessive declarations and noncanonical data.
The runtime verifier is Phase 3 and must reject unknown keys/versions without
advancing replay/chain state. JSON schema describes diagnostic serialization;
see `docs/phase2-security.md` for the CBOR wire policy.

## VerifiedReading and security events

`reading` retains the original envelope, `checked_at` records verifier time,
and `findings` permits multiple machine-readable reasons. `signature_verified`
is true only after cryptographic success, false for an attempted failed
signature, and null when no cryptographic verdict was reached.

| Status | Signature verdict | Provisional provenance eligibility |
| --- | --- | --- |
| `valid` | true | Eligible if value is present |
| `bad_signature` | false | Ineligible |
| `unknown_sensor` | null | Ineligible |
| `replay` | true | Ineligible |
| `gap` | true | Ineligible until recovery policy is agreed |
| `out_of_window` | true | Ineligible |
| `chain_anomaly` | true | Ineligible |
| `revoked_key` | null | Ineligible |
| `malformed` | null | Ineligible; no reading, digest required |

These statuses specify classification order: signature success precedes
authenticated replay/chain findings. A gap is not proof of malicious deletion.
The digest of malformed bytes is only an audit correlation identifier.

Do not ingest remotely supplied `signature_verified`, statuses or eligibility
claims. Only a trusted in-process verifier should produce these objects.
All outcomes must reach L4, even when no feature window can be constructed.

## ProcessedReading

Contains provenance, nullable normalized value/unit and quality flags. Value and
unit must be present together; provenance-ineligible input cannot have a
normalized analytical value. `ready_for_analysis` also requires normalization.
Quality flags are evidence; their semantics and exclusion policy are Phase 4
work. A true eligibility flag is not a physical-credibility judgment.

## FeatureWindow

The interval is half-open `[start, end)`. All observations must lie inside it,
with unique `(sensor_id, key_id, stream_id, sequence)` identities. Derived
datetimes accept explicit ISO timezones and normalize to UTC. Naive datetimes
and numeric epoch guesses are rejected.

`as_of` is an explicit availability cutoff at or after window end. Readings must
have been verified by that cutoff. Reference observations must predate the
window end and have been available by the cutoff. Historical references are
allowed; source-specific staleness tolerances belong to Phase 4. Forecast target
times are not modeled by this v1 observation contract and require an explicit
extension if needed. Historical evaluation must use the same causal availability
policy as deployment; a late cutoff must not silently give the detector future
information. Offline replay must preserve event time and use an injected clock.

Temporal, spatial and reference feature groups independently record
`not_computed`, `available` or `unavailable`. Unavailable groups require a reason;
they never silently substitute zeros. Available groups require named values,
unique by `(sensor_id, name)`. Nullable feature values express explicit missingness.

Reference records retain source, observation time, availability time, raw value,
unit and optional location. No weather, satellite or reanalysis source is
fabricated in Phase 1.

Feature extraction must use eligible normalized observations. Rejected readings
can remain in a window as evidence but must never contribute to analytical
features. Malformed events go directly to L4 outside observation windows.
Adapters must preserve separate evaluation labels; no label field exists in
these detector contracts. Extra fields alone do not prevent leakage in feature
names or values: evaluation review must audit feature generation too.

## L3 and L4 integration

The Layer 2 → Layer 3 adapter (`core/detection/adapter.py`, tested in `core/tests/test_adapter.py`) maps
FeatureWindows to the detector's [time, site, variable] input and checks quantity and unit compatibility.
Layer 4 (`core/response/trust.py`, tested in `core/tests/test_response.py`) consumes every Layer 1 verdict
and the detector's `score_stream` rows.

## Phase 3 runtime boundary

The runtime verifier now creates VerifiedReading after local trust lookup,
cryptographic verification, sequence/time/chain checks and durable audit commit.
Never trust a remotely supplied VerifiedReading. Wire/schema fields are unchanged.
Only `valid` non-null readings reach the optional L2 callback. All statuses reach
L4 through `ProvenancePipeline`, with a stable event ID scoped to the retained
receiver database.

## Phase 4 processing boundary

`ProcessingEngine` emits the unchanged FeatureWindow. Its separate WindowResult
contains excluded events/references, selected reference matches and the immutable
processing policy. Preserve that sidecar and the original L4 journal. Default
feature names/units and matching semantics are specified in phase4-processing.md;
the team still needs to accept them for L3/L4. Default 60-second UTC windows are
configurable prototype policy, not an official challenge parameter. Source
adapters must respect actual units, coordinates, temporal resolution and source
availability; no dataset columns are assumed.
