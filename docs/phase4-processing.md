# Phase 4: signal processing, steps 17–23

The implementation covers temperature semantics, normalization, exact UTC window
alignment, temporal/spectral features, configured-neighbor spatial features,
causal reference matching and immutable FeatureWindow construction. It preserves
the v1 signed wire format and all existing contracts. No dataset format, dataset
contents or detector performance is assumed.

Step 22's generic interface is implemented and tested. A concrete meteorological,
satellite or reanalysis source plugs in through an adapter that declares its URL, license,
columns, units, coordinate system, temporal resolution and publication time
(ERA5-Land: `data_prep/reference.py`).
The reference model currently represents point observations. Do not disguise
interval averages, grids or forecasts as point measurements: their real metadata
must guide the later adapter and any agreed contract extension.

## Processing boundary and evidence

`ProcessingEngine` accepts internal `VerifiedReading` instances emitted by the
trusted verifier, or loaded from its protected local journal. Structural Pydantic
validation is not cryptographic verification. Never deserialize a remote verdict
and treat it as authenticated input. Foreign dicts and invalid constructed models
are rejected. Raw signed envelopes and verdicts remain unchanged.

`validate_reading` returns `ProcessedReading` with normalized value/unit or null
and explicit quality flags. Only provenance-valid, nonmissing, semantically valid
readings contribute measurement statistics. Temperature quantities accept exact
units `C`, `°C`, `F`, `°F`, `K`; other units/quantities remain unnormalized evidence.
Strings, bools and integers are not coerced at this boundary. A real dataset
adapter must explicitly convert source numerical representations into the strict
internal float contract before signing/importing them.

Celsius stays unchanged; Fahrenheit uses `(F−32)×5/9`; Kelvin uses `K−273.15`.
Physical lower limits are checked in original units: −273.15 C, −459.67 F, 0 K.
The exact Fahrenheit boundary is canonicalized to −273.15 C if binary rounding
places it a fraction below that number. This does not admit inputs below the
original-unit limit. Impossible values get `below_absolute_zero`, retain raw
evidence, and have no normalized value. Large but possible temperatures are not
clipped or classified as safe. Detecting false but correctly signed data remains
L3's responsibility.

The six original semantic cases have explicit tests in `tests/test_validator.py`:
Celsius, Fahrenheit, Kelvin, incorrect types, nulls and unknown units. Missing
required fields/timestamps are rejected by the existing envelope contract and
routed by L1b as malformed evidence, never assigned an invented event time.

## Time alignment and deterministic snapshots

Default windows are nonoverlapping 60-second intervals anchored at the UTC Unix
epoch, `[start, end)`. `window_bounds` uses integer microseconds (including before
1970) and converts aware timezones to UTC. The original signed timestamp string
is retained. No clock correction, interpolation, forward-fill or synthetic
samples are introduced. Sensors with different sampling rates share a time
window; their temporal statistics are calculated separately.

`build_window(events, start=..., end=..., as_of=..., references=...)` requires one
policy-aligned interval and an aware cutoff at/after its end. Readings verified
after `as_of`, events outside the interval and malformed events are returned as
excluded evidence. Their absence from a FeatureWindow is not silent deletion.
The caller must retain that sidecar and the original L4 journal. Delayed readings
can enter an explicitly recomputed snapshot with a later cutoff; this engine does
not mutate or automatically revise previously delivered windows.

`build_closed_windows` partitions a bounded batch and returns closed-window
results plus unassigned evidence. Open windows remain unassigned for a later
caller-supplied batch. It emits no empty intervals because the expected sensor
schedule has not been supplied. There is no hidden rolling state, wall-clock
watermark or implicit backfill. Windows are deterministic under input permutation.
Their ID hashes policy and window contents, including the availability cutoff;
it is a snapshot identifier, not merely a time-bucket ID.

## Duplicates and conflicting identities

A signed identity is `(sensor_id, key_id, stream_id, sequence)`. Repeated delivery
counts once. A valid verdict takes precedence over replay/failed copies of the
same identity. A forged rejected copy cannot poison an authentic accepted sample.
All discarded copies remain in `excluded_events` with their complete raw verdict.
If two different envelopes both claim a valid verifier verdict for one identity,
the representative is retained but quarantined with
`conflicting_verified_identity`; the other version remains in excluded evidence.
This guards a corrupted integration boundary without trusting either version.

## Feature definitions

Temporal features are keyed by sensor and use only that window. Missing
statistics are null, never fabricated zero measurements. Counts/eligibility
indicators may legitimately be zero.

| Feature | Definition / unit |
| --- | --- |
| `sample_count`, `usable_count`, `missing_count`, `excluded_count` | Unique observations, normalized observations, raw nulls, and non-analytical observations |
| `mean_c`, `std_c` | Arithmetic mean and population standard deviation (denominator n), C |
| `min_c`, `max_c` | Eligible sample extrema, C |
| `rolling_count`, `rolling_mean_c`, `rolling_std_c` | Up to the last configured N samples in the final contiguous valid segment; default N=5 |
| `last_delta_c`, `last_dt_seconds`, `last_rate_c_per_s` | Last adjacent value change, positive elapsed seconds, and change/second |
| `constant_run` | 1 if the last segment has at least two samples and its range is at most the configured tolerance; otherwise 0, or null without two samples |
| `median_interval_seconds` | Median elapsed time across valid adjacent sequence pairs |
| `spectral_eligible` | Whether all spectral prerequisites hold |
| `dominant_frequency_hz` | Largest non-DC real-FFT power bin after mean removal |
| `dominant_power_fraction` | That bin's power divided by total non-DC power |

Rolling/difference calculations cannot bridge invalid/null samples, sequence
gaps, equal timestamps or key/stream changes. If the final observation is
ineligible, rolling measurement statistics are null. Whole-window mean/std remain
available for earlier usable observations. Two distinct records with an identical
timestamp remain separate raw observations but cannot form a rate or regular
spectral sequence; no arbitrary timestamp jitter is added.

Spectral calculations require at least eight samples, all normalized and in one
contiguous stream, strictly increasing timestamps and inter-sample intervals
within 1% of their median (configurable). Sampling rate comes from that median;
no irregular-series resampling is performed. Constant signals have no dominant
frequency/power fraction. This is a basic descriptive FFT, not a calibrated PSD
or detection score. Extreme values are scaled for mean/std/FFT calculations;
unrepresentable derived values become null, never JSON NaN/Infinity. Numerical
roundoff is tested with tolerances.

## Spatial evidence

Default neighbor pairs are empty: spatial features are explicitly unavailable.
Directed `(target, neighbor)` pairs must be configured by the team. Proximity
alone does not enroll a trusted neighbor. For each sensor's latest usable sample,
choose at most one latest nonfuture sample per permitted neighbor in the SAME
window. Require eligible normalized values, both locations, an age within 10 s,
and great-circle distance within 50 km (defaults, configurable).

Report `neighbor_count`, `neighbor_median_c`, `neighbor_deviation_c` (target minus
median) and `neighbor_std_c`. One sensor gets one vote regardless of sample rate.
If no sensor has usable neighbors, the group is unavailable; partial groups keep
null statistics for sensors with no matches. Signed locations establish what a
sensor asserted, not independent location attestation. Neighbor allowlists and
geography must be validated for the actual deployment. Median consensus alone
does not defeat colluding sensors.

## Reference evidence and leakage control

Adapters supply `ReferenceObservation` with source ID, observed/available times,
quantity, float/null value, unit and optional location. Sources must be explicitly
allowlisted. Raw records remain in `reference_data`; records unavailable by the
cutoff or observed at/after window end go to excluded-reference evidence.
Exact duplicate reference records are deduplicated with an exclusion record.

For each latest usable sensor sample, require a reference observed no later than
that sensor event, within maximum age (default 3600 s), compatible temperature
units, valid locations and distance within 50 km. Rank eligible records by newest
observation, closest location, newest available revision, then lexical source ID.
This tie-break is a deterministic prototype policy, not an assertion of source
quality. Equally ranked contradictory values are ambiguous and yield no match.
There is no extrapolation or spatial interpolation.

Features: `reference_c`, `reference_deviation_c` (sensor minus reference),
`reference_age_seconds`, `reference_distance_km`. Exact selected raw reference
lineage is in `WindowResult.reference_matches`. `WindowResult.policy` retains
all parameters. Reference availability is relative to detector `as_of`; a source
published after measurement but before detector execution is allowed. Historical
evaluation must use the same as-of policy rather than a retrospective advantage.

## Bounds, configuration and integration

Defaults: at most 10,000 events, 100 sensors, 10,000 references and 128 windows per
partition call. The existing FeatureWindow 10,000-reading/4,096-feature-per-group
limits are preserved. Oversized inputs fail before partial output. Configuration
is immutable and validated. `config/processing.example.json` is a template; the
CLI reads it only when explicitly passed with `--policy`. No sources/neighbors
are automatically trusted from that example.

The Phase 3 optional L2 callback deliberately filters nulls and rejected readings.
For complete Phase 4 evidence, `read_verifier_journal` reads the trusted local
journal, INCLUDING delivered events; it never deletes or acknowledges them. Its
bound fails visibly for a larger journal. Production segmentation, persistent L2
streaming state, automatic window scheduling and retention need later integration
and reliability work. Current engine calls are pure bounded snapshots.

The output is the existing `FeatureWindow` Python contract. The new WindowResult
sidecar carries exclusions, policy and reference lineage without altering that
contract. Actual L3/L4 consumer acceptance, learned feature scaling, training/test
splits and real-source adapters remain Phase 5 or later. All eight signed attack
families in the plan remain pending; none was removed. Phase 5 requires approval.
