> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Phase 3 verification evidence

Executed 2026-10-20 on Linux AMD64, CPython 3.14.8, with the unchanged hash-locked
requirements and a locally built Mosquitto 2.1.2 executable. Windows Phase 3 has
not been executed here. User-supplied Windows Phase 1 evidence remains historical.

Final command:

```text
python -m pytest -q --run-broker --broker-executable <MOSQUITTO_2.1.2_EXECUTABLE> tests
```

**Actual result: 233 passed in 6.70s; exit code 0.** No skipped tests in this run.
The complete captured pytest output is `phase3-test-results.txt`.

The suite contains the original 184 Phase 1/2 tests and 49 new Phase 3 tests.
Ten tests use a real loopback broker (eight existing transport tests plus two new
receiver integration tests). They exercise mutual TLS, ACL denials, canonical
signed CBOR, malformed input, durable replay, three sensors, the registry CLI,
the receiver CLI and restart continuity. Temporary certificates, keys and broker
processes are cleaned up by pytest fixtures. No installed service is modified.

New tests also cover signed-field tampering, wrong signing key, unknown
sensor/key/stream, revocation and atomic key rotation, duplicate enrollment,
invalid fingerprints, genesis and chain mismatch, gaps and ordered recovery,
time boundaries and timestamp regression, topic binding, concurrent duplicates,
missing/incompatible state, bounded pending events and receiver queues, no PUBACK
on failed processing, rollback on audit-write failure, and at-least-once L4/L2
consumer delivery. A legitimately signed synthetic false value remains valid
provenance, demonstrating the boundary with later physical-anomaly detection.

An earlier run before adding seven registry/routing tests passed 226 tests in
7.24s. Final results above supersede that intermediate run.

`uv pip check --python <VENV_PYTHON>` checked 20 installed packages and reported
“All installed packages are compatible.” This environment was created by uv
without pip; dependency compatibility was checked with uv rather than pip.
No dependency versions or hashes changed in Phase 3.

## Implementation review corrections

Receiver database validation checks file metadata without reading the growing
journal into memory. SQLite connections are explicitly closed, including
initialization, for Windows file-handle compatibility. A new receiver starts at
genesis; documentation uses a fresh named demo bundle instead of assuming an
already-advanced Phase 2 signer can be accepted. Existing wire schemas and
contract validation remain unchanged and their regression tests pass.

## Limits

Tests establish implemented behavior, not absence of every vulnerability.
Native Windows ACLs and the Windows broker build need user-side verification.
Default pytest runs skip 10 opt-in broker tests; Windows also skips the existing
POSIX symlink test. Full Linux testing above enables all broker tests.

Actual L3/L4 implementations, dataset integration, the standard attack/evasion
scenario, detector metrics, performance benchmarks, long-duration stability,
automatic reconnection, hardware anti-rollback and operational journal retention
remain pending. The durable callback seam is tested with synthetic consumers;
stdout is only the receiver CLI's demonstration sink.

Phases 1–3 source is included. Phase 4 is not implemented or approved by this
release. See `docs/requirements.md` and `docs/phase3-security.md`.
