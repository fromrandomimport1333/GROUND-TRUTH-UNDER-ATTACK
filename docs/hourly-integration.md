# WegenerNet hourly integration — implementation and feature contract

This release extends the supplied CIS-Team.zip, not a different unseen multivariate release.
That ZIP contains a temperature-only `adapters.wegenernet.hours` and daily
`scripts.export_wegenernet.export`; neither contained `delta_1h`, `mean_24h` or
`minus_neighbor_mean`. Both legacy functions and their tests remain. The new opt-in
hourly path implements those requested definitions, adds the other four source
variables, and uses unchanged v1 SignedReading/VerifiedReading/FeatureWindow models.
A byte-for-byte naming comparison with the team's other multivariate release is
not possible without that release; agree the explicit columns below with L3.

## Architecture and modified files

| File | Change and purpose |
|---|---|
| adapters/wegenernet.py | UTF-16/UTF-8 cell parser, real grid/schema/date preflight, chronological source iteration, source hashes and observation IDs; original adapter retained |
| core/processing/validator.py | Explicit five WegenerNet quantity/unit mappings after provenance validation; legacy temperature conversion to Celsius unchanged |
| core/processing/engine.py | Factory for opt-in hourly engine; original stateless engine untouched |
| core/processing/hourly.py | Bounded hourly multivariate history, new temporal/spatial features and v1 windows |
| core/provenance/keyregistry.py | Partial index on pending events to prevent scanning an ever-growing audit journal; backward-compatible schema/index migration |
| core/pipeline.py | Optional `include_ineligible=True` callback routing; default behavior unchanged |
| attacks/scenario.py | Offline DatasetExperiment reuses production SensorSigner, SignerState and Verifier; reproducible explicitly public experiment keys |
| adapters/feature_export.py | Streaming compact CSV, full windows and lineage exports from the same values |
| scripts/export_wegenernet.py | Dispatch to hourly mode when --cells-file is supplied; previous CLI retained |
| scripts/export_wegenernet_hourly.py | Orchestration, full verdict/wire journal, manifest and real counts |
| scripts/validate_wegenernet_export.py | Independent NumPy/raw-NetCDF checks, all compact/full window comparisons, full contract and lineage validation |
| scripts/inspect_wegenernet_flags.py | Source flag metadata and observed-value distributions |
| config/cells.txt | Exact supplied 30-cell list, original UTF-16 encoding retained |
| tests/test_hourly_integration.py | Synthetic unit/integration fixtures and opt-in real January preflight |

L1a signs; L1b verifies authentic signatures, chain, sequence and event-time
policy. L2 validates semantics and computes features. L3 remains a consumer of
FeatureWindow, and L4 receives evidence. No L3 detector or L4 behavior is changed.
No detection score, physical attack attribution success, or L3 model compatibility
is claimed; arithmetic and contract compatibility are tested.

## Source variables and units

| Raw NetCDF name / SignedReading.quantity | Unit | Quality field |
|---|---|---|
| T_zAvgTerr_HM | K | Flagged_T |
| T_zAvgTerr_HMin | K | Flagged_T |
| T_zAvgTerr_HMax | K | Flagged_T |
| RH_HM | % | Flagged_RH |
| Precip_HS | mm per source one-hour interval | Flagged_Precip |

Raw variables and units are preserved. Temperature differences and deviations
use K; differences are numerically identical in Celsius. A standard deviation of
relative humidity is in percentage points, serialized with the source `%` unit.
Precip_HS is an hourly accumulated amount, never a cumulative monthly total.
Temperature extrema reuse the temperature-family flag as contextual metadata;
the file does not supply extrema-specific flags. No extra extrema quality claim
is made. Source metadata for every variable is retained in the manifest.

The source also includes temperatures at 300 m, area aggregates, station wind,
pressure, radiation and soil temperature. These are not silently assigned to
virtual grid cells or used as independent reference data. This release implements
the five requested cell-level variables; other fields remain in the original
NetCDF and the manifest inventory, not the compact ML feature set.

## Time, availability, continuity and memory

`--start` and `--end` delimit source intervals [start,end). An interval ending at
01:00 is labeled 01:00, so January labels run from 2024-01-01T01:00Z through
2024-02-01T00:00Z inclusive. There are 744 labels. The unchanged FeatureWindow
contract has a half-open event-time envelope [label,label+1h), which contains the
unchanged signed timestamp. `as_of=label+1h`. This envelope is not the averaging
interval; source interval is [label-1h,label). Availability is an offline modeled
assumption, not actual historical publication. Source metadata says this file was
created in April 2025; it is not evidence that these published values were available
in January 2024.

Files are sorted by decoded first time, not filenames. Every file must have
hourly end-aligned bounds, compatible grid/units, no duplicate or overlapping
hours, and continuous internal timestamps. Target missing hours are a hard error
before output directory creation. Leading unavailable warm-up is reported;
interior gaps in available warm-up or target are errors, never imputed. Full-year
2024 target is 8,784 intervals. Full-year and month-boundary preflight is tested
with clearly synthetic fixtures only; real data validation is January only.

One hour of spatial fields is loaded at a time, one NetCDF is open at a time,
and L2 stores at most 169 values/IDs per selected variable plus run counters.
Audit is written incrementally. The temporary SQLite evidence journal grows on
disk for the duration of a run and is not an in-memory multi-year dataset.
The final observation journal retains all its verdicts. Output directory must be
new: no silent overwrite. Failure leaves partial files without SUCCESS.json;
these are not complete deliverables and must not be consumed as complete runs.
A fresh run is required after failure; resumable exports are not implemented.

## Temporal features

For end-labeled hourly eligible values x(t), names are `RAW_VARIABLE__FEATURE`.
Current hour is included. Missing or rejected values are null, not zero. For a
fixed window, **all required consecutive hourly samples must be eligible**.
An absent hour breaks history; a null/rejected observation breaks the relevant
windows and run. Files do not reset history. Floating equality is exact binary
float equality after source decoding; tolerance is exactly zero, with no
arbitrary sensor threshold. The file contains quantized values, so these are
runs of identical decoded measurements, not proof of a frozen physical sensor.

| Feature | Definition | First available sample without prior history |
|---|---|---|
| latest | eligible x(t) | 1 |
| delta_1h | x(t)-x(t-1) | 2 |
| mean_24h | arithmetic mean x(t-23)..x(t) | 24 |
| mean_168h | arithmetic mean x(t-167)..x(t), explicit 7-day history feature | 168 |
| std_3h | population standard deviation of x(t-2)..x(t), ddof=0 | 3 |
| std_6h | population standard deviation of x(t-5)..x(t), ddof=0 | 6 |
| run_length_identical | consecutive identical eligible samples ending at t, including current | 1, but may be left-censored |
| run_length_left_censored | 1 if beginning of observed run touches unavailable/rejected history; 0 after an observed change | 1 |
| zero_diff_count_6h | number of zero differences for pairs ending at t-5..t: 6 differences from 7 samples | 7 |
| zero_diff_count_24h | number of zero differences for pairs ending at t-23..t: 24 differences from 25 samples | 25 |
| max_abs_delta_6h | largest absolute value of those 6 consecutive differences | 7 |
| history_count | number of stored observed hourly slots, capped at 169; not count of eligible samples | 1 |
| flagged_observation_count | 1 if current raw source percentage >0, 0 if zero, null if unavailable; no quarantine effect | 1 |

Counts and binary feature values are finite floats because v1 FeatureValue
requires floats. Compact eligibility columns are booleans; audit statuses are
strings. Formulas: mean=sum(x)/N; population std=sqrt(sum((x-mean)^2)/N).
The implementation reuses numerically stable mean/std helpers. No imputation,
rounding, anomaly thresholds or forward-looking samples are introduced.

`temperature_extrema_range=HMax-HMin`. `temperature_consistent` is 1 exactly
when HMin<=HM<=HMax, 0 otherwise, null if any of the three eligible values is
unavailable. A consistency failure is a feature, not an invented verifier verdict.

## Spatial definitions

The actual coordinate reference system is EPSG:32633; X and Y increase in 100 m
steps in the attached source. Neighbors are the adjacent index cells: `ym`=(y-1,x),
`yp`=(y+1,x), `xm`=(y,x-1), `xp`=(y,x+1). Manifest maps directions to exact stable
cell IDs. These are grid directions, not assumptions about independent stations.
Central IDs are never replaced by neighbor IDs. Only the selected 30 cells emit
ML rows; all needed source neighbors are imported and verified as evidence.

| Spatial feature | Rule |
|---|---|
| neighbor_count | count of eligible simultaneous neighboring values |
| minus_neighbor_mean | central minus arithmetic mean of eligible configured neighbors; null if none or central unavailable |
| minus_neighbor_ym/yp/xm/xp | central minus that exact neighbor; null for missing/invalid/out-of-grid neighbor or central |
| minus_neighbor_median_4 | central minus median, only with at least four eligible neighbors; otherwise null |

No stale values are substituted. Boundaries naturally have fewer neighbors and
therefore no four-neighbor median. `neighbor_count` exposes the count used for the
mean. The mean formula is preserved; its neighborhood now uses actual cardinal
grid neighbors instead of all other selected demonstration cells.

Synthetic test: center=283 K and four neighbors=280 K gives +3 K median residual.
An innocent 280 K center with one neighbor=283 K and three=280 K has mean residual
-0.75 K but median residual 0 K. Two-neighbor mean reproduces -1.5 K. Per-neighbor
differences expose the asymmetric disagreement. This reduces this particular
contamination effect; it is not an attack detector or proof under collusion.

## Flags — evidence and limits

Actual field dtype is float32; units are `%`; long names describe “Fraction of
Flagged ... Data”. `_FillValue=101` is decoded as unavailable, not as 101% bad
observations. Observed values and counts are in reports/integration/flags.json.
No bitmask or binary validity semantics are supplied. A positive percentage says
that some source data were marked; it does not establish which source quality
checks failed, whether interpolated values should be discarded, or a quarantine
threshold. The original attached adapter preserved Flagged_T separately and did
not filter on it. It did not contain a `flagged_observation_count` implementation.

For the old three cells, y80_x115 has 0% for all 744 hours; y80_x116 and y80_x117
have 100% for all 744 hours. Thus >0 occurs in 1,488/2,232=66.6667% of cell-hours.
For the requested 30, >0 occurs in 97/22,320 temperature hours (0.434588%),
412/22,320 RH hours (1.845878%) and 947/22,320 precipitation hours (4.242832%).
No source flag is missing in these 30 cells. The full value distributions are
preserved. Confirm with L3 how these percentages should be used as covariates or
explicit training masks; do not reinterpret the binary descriptive count as an
L1 signature rejection or count of bad physical station measurements.

## Provenance and record identifiers

Existing `Measurement.source_id='wegenernet_v8'` identifies the dataset family,
not one sensor or one observation. It remains unchanged. The original
source_record_id used filename:variable:time-index:y:x. New hourly record IDs are
SHA-256 over the UTF-8 text:

`wegenernet-observation-v1|FILE_SHA256|VARIABLE|UTC_ISO_TIME|Y|X`

A file content digest prevents same-basename collisions; renaming/moving a file
preserves IDs. A different file version intentionally changes IDs. Variables,
cell coordinates and timestamps disambiguate observations. Duplicate source
hours are rejected instead of silently overwriting identities. This is a stable
**observation locator**, not proof of source authenticity. `file_sha256` fingerprints
file bytes; `raw_message_sha256` fingerprints exact CBOR bytes; record_hash in
core/provenance/codec.py is a domain-separated cryptographic chain digest.
These have different purposes and must not be conflated.

Observation IDs live in sidecars/compact columns, preserving strict v1 contracts.
`observations.jsonl.gz` maps each ID to original file digest, variable, index,
coordinates, raw value, raw flag, full VerifiedReading and exact wire CBOR hex.
`lineage.jsonl.gz` maps each window to current IDs, rolling-history ranges, per-neighbor
IDs and identical-run start IDs. A history range stores slot count, first/last ID
and missing offsets; sensor, variable and hourly timestamps reconstruct every
intermediate ID uniquely from the complete observation journal. This avoids
repeating 169 random hashes for every variable on every row. It links historical evidence instead of copying
whole source records into each feature window. The public-key enrollment supports
independent verification/replay of the retained wire sequence. Experiment keys
are deliberately reproducible and public test keys; never enroll them as live
trusted physical sensors. This authentication claim is explicitly limited to the
experiment and is not WegenerNet station authentication.

## Attack, replay, missingness and references

| Question | Answer grounded in the implemented path |
|---|---|
| Original or potentially attacked values? | L2 reads ProcessedReading from the verified signed payload, not a clean baseline. attacks/scenario.py signs pre-sign modifications; a correctly signed +3 K alteration reaches features. No baseline is injected into L3. |
| Verifier rejects? | Verifier retains verdict in journal; semantic validator sets normalized value to null. Hourly import passes all verdicts to L2 and audit. |
| Missing observations? | Explicit nulls become missing analytical values; absent sensor/quantity in a supplied hour produces an explicit missing compact status and null features. Missing target source hours stop offline export before any successful output. |
| Replay? | Production verifier returns replay after signature verification; stream head does not advance; that observation is ineligible. Full verdict remains in audit. |
| Can invalid values enter rolling statistics? | No: only ready_for_analysis values enter numeric history. Null slots break strict windows and identical runs. |
| What does L3 receive? | Hourly mode emits a window/row with null features and source statuses, retaining rejected current readings. Malformed events remain outside FeatureWindow in audit. Legacy live callback remains eligible-only by default; opt into include_ineligible=True to feed all verdicts to an hourly consumer, and supply explicit hourly ticks for absent hours. |
| reference_features/reference_data? | New mode explicitly uses unavailable/independent_source_not_supplied and empty tuple. Legacy reference matching requires caller-supplied ReferenceObservation and configured sources. |
| Independent satellite/reanalysis source? | None is implemented or supplied. Same-grid neighbors are spatial context, never independent references. No external data were fetched. |

The opt-in callback does not invent a live scheduler or complete production
streaming service. Full source-file export and tests are implemented; application
owners still supply hourly scheduling and audit persistence for a live deployment.
Duplicate sensor/quantity/time input is rejected by HourlyEngine as ambiguous;
all wire events must remain in the audit journal, and replay/dedup selection belongs
before this hourly one-reading-per-slot interface. No overwritten evidence.

## Exports and missing values

`compact.csv.gz`: one selected cell-hour per row, stable chronological timestamp
then sorted cell ID ordering. CSV null is an empty field. Float values preserve
round-trip Python precision. Booleans are True/False. ID/status/quality strings
are strings; no nested large provenance objects. `compact-schema.json` enumerates
all columns, types and units. Source values/features use variable prefixes;
per-variable status, eligibility, raw flag percentage, quality flags and current
observation ID supplement the features.

`windows.jsonl.gz`: gzip of complete standard v1 FeatureWindow JSON objects,
one per selected cell-hour. Central and four-neighbor current readings retain
full signed envelopes/verdicts. Historical lineage is in the sidecar and all
original audit records are in observations.jsonl.gz. Null is JSON null, never
NaN. Existing consumers can decompress then call FeatureWindow.model_validate_json.
The legacy daily uncompressed windows.jsonl mode also remains available.

`manifest.json`, `FeatureWindow.schema.json`, experiment public keys, lineage,
`SUCCESS.json`, `independent-validation.json` and measured file sizes complete
the handoff. A compact export alone is deliberately not the full audit package.
L3 should use compact rows for training and retain all audit files for traceability.

## January warm-up limitations

No December observations were supplied or fabricated. With 30 cells and clean
hourly values, unavailable counts **per variable** are:

| Feature | First eligible January label | Missing initial cell-hours |
|---|---|---|
| delta_1h | January 1, 02:00 UTC | 30 |
| std_3h | January 1, 03:00 UTC | 60 |
| std_6h | January 1, 06:00 UTC | 150 |
| zero_diff_count_6h, max_abs_delta_6h | January 1, 07:00 UTC | 180 each |
| mean_24h | January 2, 00:00 UTC | 690 |
| zero_diff_count_24h | January 2, 01:00 UTC | 720 |
| mean_168h | January 8, 00:00 UTC | 5,010 |

These are theoretical minimum missingness counts; the real execution's
SUCCESS.json lists measured null counts including any actual source gaps/invalid
values. Identical runs that begin at the first available hour are lower bounds
until a different eligible value is seen. Seven days of warm-up cannot establish
the true start of a run already in progress before that history either.

## Remaining coordination with Layer 3

Confirm column names/order, K versus the other release's temperature convention,
strict complete-window rules (especially 6 differences=7 values), four cardinal
neighbors, exact equality, flags/masks, left-censored runs, envelope versus source
interval timestamps, and missing-value policy. Confirm model input schema before
training. Seasonal evaluation, annual completeness, December warm-up and genuine
four-layer detection evaluation remain unavailable. No claims extend beyond the
attached January source and explicitly labeled synthetic tests.
