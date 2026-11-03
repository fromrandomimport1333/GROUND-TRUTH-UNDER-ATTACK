# Phase 4 on Windows PowerShell

The cumulative archive contains Phases 1–4. Extract into
`E:\CIS_Challenge_Phase1`, merging the inner `CIS-Challenge` source folder.
Keep the parent `.venv`, private keys, signer databases and receiver databases.
No package versions or hashes changed in Phase 4.

Open the inner project in VS Code and select your existing interpreter:
`E:\CIS_Challenge_Phase1\.venv\Scripts\python.exe`.

## 1. Test

```powershell
cd "E:\CIS_Challenge_Phase1\CIS-Challenge"
..\.venv\Scripts\python.exe -m pytest -q
```

The default run skips real broker tests and the POSIX-only symlink test on Windows.
For all broker tests:

```powershell
..\.venv\Scripts\python.exe -m pytest -q --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

Tests use temporary credentials and an ephemeral broker port; they do not replace
existing local bundles. Linux test results do not certify native Windows behavior.
Your Windows verification for Phases 2–4 remains pending until you run these checks.

## 2. Run Phase 4 immediately, without broker setup

```powershell
..\.venv\Scripts\python.exe -m scripts.demo_processing
```

Expected success marker: `PHASE 4 DEMO PASS`. The demo generates temporary real
Ed25519 keys, signs/verifies 48 synthetic readings across three sensors, normalizes
C/F/K values and emits a FeatureWindow with temporal, spatial and reference
features. Its reference source is explicitly `synthetic_demo`; it is not official
weather data. Temporary credentials are removed when the demo finishes.

## 3. Process a real local Phase 3 receiver journal

First perform the Phase 3 live demonstration described in `windows-phase3.md`.
Retain its `local-phase3/receiver.sqlite3`. Inspect the signed timestamps and use
a UTC minute containing those readings. Set `as_of` to a time at/after the window
end and all desired verification times. For example, replace these sample times
with your actual data interval:

```powershell
..\.venv\Scripts\python.exe -m scripts.build_feature_window --state local-phase3/receiver.sqlite3 --start "2026-10-20T12:00:00Z" --end "2026-10-20T12:01:00Z" --as-of "2026-10-20T12:02:00Z"
```

This reads the trusted journal without modifying it and prints a JSON object
containing the policy, FeatureWindow and excluded-event evidence. The minute must
match actual signed data; the example does not promise readings at those times.
Absent geographic metadata/reference sources produce explicit unavailable groups.

To customize policy, copy `config/processing.example.json`, edit its values, and
pass `--policy config/your-processing.json`. With that option, set window duration
in the file rather than also passing `--window-seconds`. These are local policy
settings, not a dataset format. Real dataset adapters will be defined from actual
source documentation in Phase 5.

Use Python `ProcessingEngine.build_window(..., references=...)` for reference
adapters. See `phase4-processing.md` for inputs, formulas, bounds and evidence
handling. Phase 5 has not started.
