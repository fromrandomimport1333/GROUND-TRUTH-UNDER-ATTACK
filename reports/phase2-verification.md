> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Phase 2 verification results

The release was tested on Linux x86_64 with CPython 3.14.8 and the checked-in
Python dependency lock. Mosquitto version 2.1.2 was built from its official
source and used for real loopback MQTT v5/mutual TLS tests. This is not a mock
broker result. Exact command summaries are in `phase2-test-results.txt`.

## Actual results

| Check | Result |
| --- | --- |
| Complete suite including actual broker tests | 184 passed, no skips or failures |
| Real broker subset | 8 passed |
| Offline three-sensor signing demo | Passed: signatures, CBOR round trips, chains and tampering check |
| Python dependency consistency | `pip check` returned exit 0 |
| Phase 1 regression coverage | All original 114 tests remain included and passed |

The non-broker portion contains 176 tests. Default `pytest -q` explicitly skips
the 8 opt-in broker tests. A POSIX symlink test is skipped on native Windows;
the actual Windows Phase 2 summary must be recorded from that run, not inferred
from Linux counts.

## What the real broker tests exercised

1. Three distinct sensor certificates publishing actual Ed25519-signed CBOR;
   the received bytes round-trip and verify against the sensor public keys.
2. Sensor certificate denied publication to another sensor's topic.
3. Read-only observer denied publication.
4. Untrusted server CA rejected.
5. Wrong server hostname rejected.
6. Client without a certificate rejected.
7. Client certificate from an untrusted CA rejected.
8. Actual simulator and observer command-line programs exchanged nine records,
   followed by a restart and three more records with continuing sequences.

Tests generated temporary credentials and launched their own broker on an
ephemeral loopback port. Those credentials and the broker build are excluded
from the archive.

## Problems found and corrected

Initial strict Python 3.14 TLS validation rejected certificates missing Authority
Key Identifier extensions. CA/leaf Subject Key Identifier and Authority Key
Identifier extensions were added; certificate verification remained enabled.
The corrected positive and negative real broker tests passed.

The original environment lacked cJSON development headers. Package-manager
installation could not write its system cache, so the matching development
package was downloaded/extracted into scratch for compilation. Only the broker
target was needed; optional documentation, WebSockets, editline, SQLite plugin
and HTTP administration API were disabled in this temporary test build. TLS,
MQTT v5 and ACL behavior were enabled and exercised. No system-wide service
or public listener was installed. Windows uses the user's installed 2.1.2
distribution; its native Phase 2 execution is not claimed here.

## Other automated coverage

Tests cover every signed payload field, public fixed protocol/RFC vectors,
signature exclusion versus chain inclusion, genesis, independent sensors,
concurrent allocations, transaction rollback, clock regression, sequence
exhaustion, pending-record backpressure, retrying identical bytes, exact-payload
acknowledgement, restart continuity, missing-state refusal, unprovisioned streams,
encrypted key loading, incorrect passwords, exclusive file creation, portable
IDs, local manifest path boundaries, CA/client/server certificate roles and
bounded canonical CBOR parsing.

## Verification boundaries

Windows Phase 1 was verified from user-supplied output: 114 tests passed and all
environment targets matched. See `windows-phase1-validation.md`. Native Windows
Phase 2 ACL and broker results remain pending execution of
`docs/windows-phase2.md`.

Layer 1b runtime provenance verification, replay/registry policy, Layer 2 signal
processing, actual L3/L4 integration, dataset evaluation, detection
metrics, automatic reconnect stability and benchmarks are later phases. No
challenge detection result or performance claim is made by the 184 tests.
