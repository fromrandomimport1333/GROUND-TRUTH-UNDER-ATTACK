> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Phase 5 verification — 20 October 2026

## Actual execution

The cumulative source was tested on Linux with CPython 3.14.8 and the existing
hash-locked dependency environment, using a real local Mosquitto 2.1.2 executable.

Command from the project root (executable paths shortened for portability):

```text
python -m pytest -q tests --run-broker --broker-executable PATH_TO_MOSQUITTO
```

**Actual result: 361 passed in 15.69s; no skipped or failed tests.**
There are 43 new Phase 5 cases (including one real broker integration), with all
318 existing Linux cases retained. All 12 broker tests ran.

The earlier focused Phase 5 run returned 42 passed, 1 skipped in 7.98s. Its broker
test was intentionally skipped because that run did not use `--run-broker`.
The cumulative broker-enabled run supersedes that skip.

## What the tests establish

- Eight configurable pre-sign attack families have deterministic transformations,
  explicit intervals/targets, source preservation and separate labels.
- Exact bias/drift/pulse/freeze/flatten/template/masking/collusion behavior is
  checked; noise changes with seed and remains stable across sensor interleaving.
- Nulls, invalid configuration, wrong units, overflow, ordering and batch limits
  are exercised without fabricating measurements.
- All eight signed families pass real Ed25519 verification and reach L2 features
  and a validated FeatureWindow receiver. Labels do not enter detector inputs.
- Tampering, unknown keys, replay, missing/reordered predecessors, signed chain
  breaks, stale timestamps and malformed wire bytes produce tested statuses.
- Rejected messages do not poison continuity; valid retry can restore a chain.
- Replaying the same recorded bytes in two fresh registries produces equal
  verdicts, features and window IDs. Used registries cannot be silently reset.
- L4 failure leaves a durable pending event; L3 failure propagates and permits
  explicit redelivery of a retained window.
- Real MQTT v5/mTLS carries a tampered message, validly signed false measurement
  and replay through the production receiver into audit and feature processing.
- The actual demo CLI exercises eight signed families and six wire scenarios.

## Windows evidence

The user previously ran the cumulative Phases 1–4 suite on Windows with Python
3.14.0 and Mosquitto 2.1.2: **317 passed, 1 skipped in 23.43s**. The skipped case
was the existing symlink privilege test. The long binary pytest-ID fix is retained.
This is user-reported evidence, not a Windows execution performed here.

**Native Windows Phase 5 execution is still pending.** See docs/windows-phase5.md.

## Remaining dependencies

Real dataset adapters, standard scenario integration, team acceptance of
the L3 contract, actual detector/response integration and detection evaluation
remain pending. All demo data/templates are synthetic. No detection success,
false-positive rate, resource benchmark or final challenge compliance is claimed.
Development Phases 6 and 7 have not been started.
