# Phase 6: measurement, bounded recovery and evaluation tooling

This phase covers development steps 33–36. Measurements come from actual runs;
synthetic sensor inputs remain explicitly identified. Real detector evaluation
requires Layer 3 and agreed labeled datasets. See reports/phase6-verification.md
for results, platform and the duration of the executed stability run.

## Latency and controlled comparisons

`benchmark.py` uses `perf_counter_ns` to time warm-up iterations and multiple
measured series. Each stage reports the mean, minimum, maximum, p50, p95 and p99,
with the number of observations. Percentiles use nearest rank. Pooled results
combine actual samples, not averages of per-series percentiles. Small samples
produce coarse percentiles; values are not guaranteed deployment latency.

The offline benchmark covers:

- Ed25519 signing and verification on prepared canonical signing bytes.
- Production canonical CBOR encoding and bounded decode/contract validation.
- Encoding-only signed/unsigned CBOR and JSON with hex binary fields.
- Chain record hashing, including production encoding.
- Structural contract validation without cryptographic assurance.
- Sixteen-reading normalization alone and full Layer 2 window construction.
- Signing with chain state and a durable SQLite outbox.
- Verification with decoding, registry/replay/chain policy and durable audit.

Durable stages use new sequences for every measurement. Outbox/audit
acknowledgements occur outside their individual timers. Signing and verification
include filesystem costs; primitive cryptographic measurements exclude them.
No production switch disables signatures, TLS or chain validation.

Signed/unsigned CBOR comparisons report both byte sizes and encoding cost. The
unsigned control removes only signature and predecessor-hash fields and does
not offer equivalent security. CBOR/JSON encoding comparisons use prepared data
structures; JSON's binary fields are hex. Full Layer 2 versus normalization-only
includes alignment, feature calculation and window construction, so the reported
difference is not presented as the isolated cost of one feature.

With `--broker-executable`, a new temporary broker binds ephemeral loopback
ports. Its TLS listener uses the existing mutual-TLS/ACL configuration. A second
loopback-only plaintext listener is an evaluation control for identical synthetic
payloads; it is never added to a user's existing broker or deployment config.
The report measures new client setup to CONNACK (including TLS-context/certificate
loading), publication to broker PUBACK, and publication to subscriber callback.
Subscriber latency uses a shared monotonic clock in one process. It does not
include verification or processing. PUBACK is not end-to-end application receipt.
Modes run sequentially, without randomized order, so small differences may be
noise rather than causal TLS overhead. The broker process is stopped on exit.

## CPU and memory

Resource measurement is a separate pass so `tracemalloc` does not contaminate
reported latency stages. The workload verifies and decodes a signed reading and
builds a sixteen-reading feature window. Reports include process CPU seconds,
wall seconds and CPU usage relative to one core; values above 100% are possible
for a multithreaded process. Broker CPU/memory are excluded.

`tracemalloc` reports Python allocations during that pass, not total native memory.
Current RSS and lifetime peak RSS use Linux proc/getrusage or Windows process
memory counters. Unsupported/unavailable observations are null, not zero. The
lifetime peak is not described as memory uniquely allocated by the measured stage.
No CPU model, username, hostname or absolute local paths are written to reports.

## Reconnection and retained evidence

`ReconnectingPublisher` retries the exact payload after connection/PUBACK failure.
Defaults: five attempts, exponential delay starting at 0.25s capped at 2s.
Each connection also has its configured timeout. Certificate/authentication and
validation failures propagate. Duplicate delivery after uncertain PUBACK is
expected and is handled by the persistent verifier. Retries never re-sign data.

`ReconnectingReceiver` re-establishes a subscription after a transport disconnect,
using the same verifier database. It drains pending L4 audit events. Idle timeout,
queue overflow and application sink failures propagate rather than being hidden
as ordinary reconnects. Consumers still deduplicate stable local event IDs.

The deployed transport continues to use clean sessions and a nonpersistent
broker. Messages acknowledged while a receiver is offline can therefore be lost
to the receiver. The optional `RecoveryArchive` retains exact signed bytes before
publication. Sender outbox deletion occurs only after a successful broker PUBACK,
and the archive survives this deletion. If a crash occurs between archive retention
and outbox acknowledgement, retaining the same bytes again is idempotent.
Conflicting bytes for the same signed identity fail visibly.

The archive is a separate, explicitly created private SQLite file. It has a hard
record capacity (default 10,000), never silently overwrites a file, and never
automatically deletes old evidence. Once full, publication stops and the sender
record remains pending. Operators must plan archive retention/export before
long-running deployment; automatic deletion/rotation is not implemented. Receiver
audit history also grows on disk. The soak reports these sizes rather than
claiming indefinite storage stability.

For an existing live bundle, create the archive explicitly, then opt in:

```text
python -m scripts.manage_recovery --archive local/recovery.sqlite3 create --capacity 10000
python sensor.py --local local --recovery-archive local/recovery.sqlite3
python -m scripts.receive_mqtt --local local --state local/receiver.sqlite3 --count 100 --timeout 60
```

Inspect retained capacity or resend an explicit half-open sequence range:

```text
python -m scripts.manage_recovery --archive local/recovery.sqlite3 status
python -m scripts.manage_recovery --archive local/recovery.sqlite3 resend --local local --sensor sensor_01 --start 5 --stop 8
```

The entire range must exist before anything is sent. Choose the range using
trusted receiver gap/head information. Resending does not modify receiver trust
state, stream enrollment, timestamps, signatures or replay policy. The command
reports broker acknowledgement, not receiver acceptance. Inspect actual verdicts.
A late record beyond the default 120-second freshness bound is still rejected;
an extended outage may require explicit enrollment of a new stream. Recovery
cannot reconstruct records absent from the archive. Local privileged database
rollback and hostile disk replacement remain outside the software trust anchor.

## Stability harness

`stability.py` owns a temporary broker and three sensors publishing concurrently
with independent keys/chains. It runs for a configured duration or round cap,
whichever is reached first. Defaults: 60 seconds, 0.2-second pause between rounds,
maximum 2,000 rounds. Duration is bounded at one hour; interval includes a pause
in addition to actual work, so it is not a guaranteed sample rate.

Faults run once when enough rounds are reached:

1. Observer outage: successful broker PUBACK while offline, then a missing
   predecessor (`gap`), then retained-byte recovery in sequence.
2. Owned broker restart: publishers reconnect without resetting sequence state.
3. L4 sink interruption: the committed audit event survives receiver restart;
   retransmission yields one replay and the remaining valid records.
4. Duplicate delivery: the verifier reports replay.
5. Signer state reopen: sequence/hash continuity persists.

Additional real-broker integration tests exercise automatic receiver reconnection.
At completion the harness checks sender/receiver sequence heads match, every
issued record has a valid audit event, and no sender or receiver work is pending.
Only closed event-time windows go through Layer 2; the current open window remains
explicitly unassigned. Memory snapshots, CPU time, database sizes and exercised
faults are recorded. Very short runs may not reach every fault, and the report
states this. A two-minute run is limited evidence, not a multi-hour soak or proof
of infinite uptime. The process always cleans up the broker it started and never
stops a system Mosquitto service.

## Detector metrics: ready for real predictions, evaluation pending

`eval/metrics.py` accepts evaluation-only `LabeledDecision` records and computes
TP/TN/FP/FN, detection rate, false-positive rate, precision, F1 and accuracy.
Undefined denominators return null. Duplicate IDs, nonboolean labels and missing
score/label matches fail. `threshold_decisions` requires an explicit finite
threshold and exact matching IDs. Choose thresholds on training/validation data,
not the test set. Define the evaluation unit (reading/window/incident) with Layer 3.

Metrics tests use small declared examples to check formulas; they are not challenge
results. No detector predictions or labels are fabricated for a performance claim.
Detector evaluation on real data was done later (see `core/docs/README.md`).
