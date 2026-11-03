# Half-hourly integration

This extends the latest hourly release. L1 signing/verifying and L2 semantic
validation remain separate. L3/L4 and strict v1 contracts are unchanged. The
explicit entry point is `python -m scripts.export_wegenernet_halfhourly`.
The legacy hourly and daily entry points remain supported.

## Source and time

The half-hour source uses `T_zAvgTerr_HHM`, `T_zAvgTerr_HHMin`,
`T_zAvgTerr_HHMax` (K), `RH_HHM` (%) and `Precip_HHS` (mm accumulated in
30 minutes). No resampling, duplicate hourly values or unit conversion is used.
Every supplied file is preflighted for EPSG:32633, identical increasing X/Y,
exact dimensions/units, 1,800-second bounds and consecutive end labels.
Overlaps and target gaps fail before output creation. Missing leading warm-up
is reported, never filled. Dates require UTC-aligned 30-minute boundaries.

Target dates describe source intervals [start,end). Labels are start+30min
through end, inclusive. January 2023 labels run from January 1 00:30 through
February 1 00:00. A final midnight belongs to the preceding source month.
FeatureWindow is [label,label+30min), with as_of=label+30min to preserve the
existing contract containing each reading's unmodified timestamp. This is an
offline availability assumption, not proof of publication time.

## Temporal definitions

Current sample included; all specified consecutive samples must be eligible.
No imputation, tolerance or arbitrary sensor threshold. Equality is exact on
decoded source floats. Population std uses ddof=0. Units match the source;
count/boolean feature values are floats to preserve v1 contracts.

| Feature | Required samples | Definition |
|---|---:|---|
| latest | 1 | Current eligible value |
| delta_30min | 2 | Current minus previous value |
| delta_1h | 3 | Current minus value two steps earlier; middle must be eligible |
| std_3h | 6 | Population standard deviation |
| std_6h | 12 | Population standard deviation |
| mean_24h | 48 | Arithmetic mean |
| mean_168h | 336 | Arithmetic mean |
| zero_diff_count_6h | 13 | Number of zero adjacent differences, 12 comparisons |
| zero_diff_count_24h | 49 | Number of zero adjacent differences, 48 comparisons |
| max_abs_delta_6h | 13 | Largest absolute adjacent **30-minute** difference |
| run_length_identical | Ongoing | Number of consecutive identical eligible observations |
| run_length_left_censored | Ongoing | 1 until observed change establishes a run beginning |
| history_count | Up to 337 | Stored slots, including null slots |

A run of N identical values spans (N-1)*30 minutes between endpoints. Run count
is not elapsed hours. Missing/rejected values break runs and complete windows.
An absent input tick resets temporal history; source export rejects missing
interior ticks. Continuous source files, months and years never reset the engine.
Seven days of real preceding samples initialize history when available. A run
already in progress at the start of warm-up remains left-censored.

Extrema range is HHMax-HHMin. Consistency is HHMin <= HHM <= HHMax, null if
any input is ineligible; it is a feature, not an L1 rejection.

## Spatial and quality behavior

Four cardinal grid cells use stable ym/yp/xm/xp directions and identities in the
manifest, even outside the output selection. Only simultaneous eligible values
are used. Mean residual uses available peers and reports their count. Median
residual requires four valid peers; at a boundary or with fewer peers it is null.
Per-direction differences are null for unavailable peers. A synthetic +3 K
attack produces +3 K central median residual; an innocent center with one
attacked neighbor and three clean neighbors has median residual 0 K. This is
arithmetic behavior, not a detection performance claim.

Flagged_T/RH/Precip are float32 percentages with fill 101 and metadata describing
a fraction of flagged data. They are retained separately; >0 is only a
descriptive binary flagged_observation_count. No justified rejection threshold
is supplied. These percentages do not invalidate a verified signed observation.

## Provenance and audit

source_id remains wegenernet_v8 (dataset family). Observation ID is SHA256 of
`wegenernet-observation-v1|FILE_SHA256|VARIABLE|UTC_ISO_TIME|Y|X`, distinct from
wire hashes and signed chain hashes. Moving a source file preserves this ID;
changing file bytes changes it. L2 uses eligible verified payload values, including
any signed attack, never a hidden clean reference. Rejected values stay in audit
but become null analytically. Replay remains ineligible. Malformed events remain
in the all-verdict journal, and duplicate cell/variable/tick input fails explicitly.
References remain unavailable; no external satellite or reanalysis data is added.
Experiment keys are public deterministic OFFLINE TEST keys, not WegenerNet
sensor authentication. Do not deploy them as live credentials.

## Export and restart

One directory per source month contains compact.csv.gz, windows.jsonl.gz,
lineage.jsonl.gz, observations.jsonl.gz, schemas and SUCCESS.json. CSV has one
row per cell/tick, timestamp then sorted cell ID; empty fields mean null, booleans
are True/False, floats retain round-trip precision. JSON uses null, not NaN.
The schema lists every column's type and unit. Compact and full values come
from the same FeatureWindow. Full windows contain current central/neighbor
readings; journal/lineage retain all historical evidence without repetition.

The root contains the source manifest, input identity, public keys and separate
warmup-observations.jsonl.gz. Keep every partition and the root together. A
history range uses its explicit interval_seconds=1800, slot count, endpoint IDs
and missing offsets to reconstruct references through the journals across files.
Run start IDs may reference a much earlier partition. A monthly ZIP alone is
not a self-contained historical audit.

--resume verifies hashes of completed partition files and unchanged source
content/configuration. It replays from the earliest supplied warm-up using the
same public experiment keys, reconstructing L1 chains, L2 history and identical
run state. Completed partitions are retained; an incomplete partition is rebuilt.
This is resumable deterministic replay, **not a fast serialized-state checkpoint**.
Reprocessing earlier inputs costs time but bounds L2 memory and avoids restoring
untrusted serialized objects. Never consume a partition lacking SUCCESS.json.
No simultaneous writers to one output directory are supported.

Preflight loads time coordinates, not whole multiyear spatial arrays. Processing
loads one spatial tick and at most 337 history slots per selected series. The
SQLite verifier audit grows on disk within a run; exported journal retains all
its evidence. The independent validator currently indexes all observation IDs
in memory: run it on manageable supplied periods if full-year memory is limited,
and report its exact validation scope rather than claiming annual validation.

## Changed files

adapters/wegenernet.py adds cadence/schema selection; core/processing/hourly.py
uses cadence-aware durations while retaining its import/API; validator.py adds
half-hour quantities. feature_export.py adds explicit lineage cadence.
scripts/export_wegenernet_halfhourly.py orchestrates monthly writes/replay resume;
validate_wegenernet_export.py independently computes cadence-aware expectations.
tests/test_halfhourly_integration.py adds synthetic fixtures and optional real
source preflight. Existing tests and strict contracts are not changed.

## Scope

Only January 2023 half-hourly source is available here. The hourly January 2024
file is not used for half-hourly production. December 2022 and the other 23
half-hourly months are absent. Full 2023/2024 processing is therefore not validated.
The theoretical two-year target is 35,088 intervals and 1,052,640 rows for 30
cells, not a measured output count. See reports/half-hourly for actual execution.

Reproducibility refers to decoded records and values; gzip creation timestamps
and measured runtime may differ between runs. RH deviations are percentage
points serialized with source unit %. Precipitation means are means of the
half-hour amounts, not accumulated rainfall over the trailing window.
