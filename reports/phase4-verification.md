> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Phase 4 verification evidence

Executed 2026-10-20 on Linux AMD64 with CPython 3.14.8, Mosquitto 2.1.2 and the
unchanged hash-locked requirements. Native Windows Phases 2–4 have not been
executed here; the user's earlier Windows Phase 1 results remain historical.

Final source-tree command:

```text
python -m pytest -q tests --run-broker --broker-executable <MOSQUITTO_2.1.2_EXECUTABLE>
```

**Actual result: 318 passed in 9.58s, exit code 0.** No failures or skips.
The captured command/output is retained in `phase4-test-results.txt`.

The suite includes 233 existing tests plus 85 new Phase 4 tests. Eleven tests use
a real local MQTT/TLS broker, including a new path from sensor signing through
MQTT, L1b verification, the durable journal and Layer 2 FeatureWindow construction.
Temporary credentials and broker instances are isolated from user provisioning.

New coverage includes all six required semantic validation cases; temperature
boundaries and wrong types; immutable raw evidence; conflicting/duplicate events;
malformed and unavailable events; UTC half-open intervals and timezone handling;
mixed sample rates and partitioned windows; known mean/std/delta/rate calculations;
rolling continuity and missingness; a known sinusoid frequency; constant/irregular
signals; extreme numerical inputs; bounded input sizes and invalid policy;
configured-neighbor membership, geographic/time bounds and one-vote-per-sensor;
reference conversion, causal/availability cutoffs, revisions and ambiguity;
reference lineage; JSON contract round trips; the actual journal-processing CLI;
and the standalone signed synthetic demo.

`uv pip check --python <VENV_PYTHON>` checked 20 installed packages and reported
“All installed packages are compatible.” No dependency lock changes were made.

## Findings corrected during implementation

The first Phase 4 unit run reported 73 passes and two failures. One exposed
binary floating-point rounding at exactly −459.67 F: checking the converted
Celsius value could incorrectly label absolute zero as physically impossible.
The implementation now validates the original-unit lower limit before conversion
and canonicalizes boundary roundoff. The other failure was an exact-equality
assertion for a floating-point standard deviation; that assertion now uses an
appropriate numerical tolerance. Both pass in the final suite.

Subsequent review preserved all journal events for window evidence because the
existing optional analytical callback excludes signed nulls/rejections. Arithmetic
is scaled to handle extreme finite inputs; overflowing derived rates become null.
Reference selection returns exact source lineage in a sidecar. The existing v1
SignedReading, VerifiedReading and FeatureWindow schemas were retained.

## Status and limits

Steps 17–21 and 23 are implemented and tested. Step 22's source-independent
interface and matching behavior are implemented/tested; authentic weather,
satellite or reanalysis sources and their concrete adapters remain pending.
Synthetic fixtures and the synthetic_demo source are not real weather data.

Phase 4 operates on bounded snapshots, with explicit time intervals and as-of
cutoffs. Production streaming persistence/scheduling, larger-journal segmentation,
source-specific temporal/spatial aggregation, learned scaling and actual L3/L4
integration remain later work. No automatic resampling or invented data is used.

Actual challenge attack/evasion evaluation, detector metrics, benchmarks,
long-duration stability and Windows verification remain pending. Default tests
skip 11 opt-in broker tests; Windows additionally skips the existing POSIX-only
symlink test. This Linux run explicitly enabled every broker test.

Phase 5 is not implemented by this release and requires separate approval.
