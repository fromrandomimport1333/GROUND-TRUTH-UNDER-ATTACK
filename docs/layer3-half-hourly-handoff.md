> **Historical hand-off note** from Layer 2 to Layer 3, written before the detector was trained on the full 2023–2024 data. Current detector: [core/docs/README.md](../core/docs/README.md).

# Layer 3 handoff

The new workflow emits one row per selected cell per 30-minute source interval.
Read each monthly compact.csv.gz with pandas.read_csv; empty values mean null.
Use compact-schema.json and retain root manifests plus every monthly audit file.
End labels are preserved; final midnight belongs to the preceding source month.

Source names now use HHM/HHMin/HHMax, RH_HHM and Precip_HHS. Temperature stays
K; precipitation is millimeters per 30 minutes. delta_30min is new; delta_1h
still means one hour. std_3h/std_6h use 6/12 samples, mean_24h/mean_168h use
48/336, zero-difference counts use 12/48 adjacent half-hour differences, and
max_abs_delta_6h uses 12 half-hour differences. Strict complete eligible history
is required. Identical-run count remains a sample count with left-censoring.

Spatial mean, four-direction differences and strict four-neighbor median are
retained. Source flag percentages are covariates, not verifier rejections.
Observation IDs and historical lineage remain traceable to original records.
No independent cross-source reference is available.

Please confirm updated feature names/order, cadence, missing-value masks,
precipitation interpretation and model preprocessing before training. Only
January 2023 half-hourly source was supplied here; annual/seasonal performance,
remaining months and early-2023 December warm-up cannot yet be evaluated.
