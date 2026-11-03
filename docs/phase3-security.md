> Phase 3 provenance policy. Phase 4 processing is now available in
> phase4-processing.md; actual L3/L4 consumers remain pending.

# Phase 3: provenance verification and integration policy

Development steps 11–16 are implemented. Existing SignedReading, VerifiedReading,
FeatureWindow and canonical CBOR wire schemas are unchanged. No dataset format,
physical validity rule or L3 detector is introduced.

## Dependencies and trust boundary

`live_mqtt` uses Phase 2's mutual TLS transport and observer read-only identity.
The same public registry bundle initializes a separate receiver SQLite database.
Sensor signing state and receiver verification state must never share a database.
The registry stores public Ed25519 bytes, sensor/key/stream bindings, revocation
flags, per-stream sequence/hash heads, and a durable provenance event journal.
The receiver needs public sensor keys and its own TLS identity, not sensor private
keys. Local development colocates credentials; deploy separate accounts/hosts
and provision only the necessary material when separating trust domains.

Enrollment is a trusted local operation. Fingerprints must match public keys;
keys and streams are never learned from messages. Enrollment cannot overwrite a
head. Revocation applies to every stream using the sensor/key binding and cannot
be undone. `enroll --retire-key` atomically enrolls a replacement and revokes its
predecessor. It does not automatically generate or distribute replacement signer
credentials. Provision the signer and authorize its new stream explicitly.

## Verdict precedence

| Check | Status / finding | Advances head? |
| --- | --- | --- |
| Invalid/incomplete/noncanonical CBOR | `malformed`, original-payload SHA256 | No |
| Sensor/key absent | `unknown_sensor` | No |
| Enrolled key revoked | `revoked_key` | No |
| Stream not enrolled | `unknown_sensor` / `stream_not_enrolled` | No |
| Ed25519 failure | `bad_signature` | No |
| MQTT topic mismatches signed sensor | `chain_anomaly` / `topic_identity_mismatch` | No |
| Sequence below expected | `replay` / `sequence_already_consumed` | No |
| Time outside configured bounds | `out_of_window` | No |
| Sequence above expected | `gap` / `missing_predecessors` | No |
| Expected sequence, wrong predecessor | `chain_anomaly` / `previous_hash_mismatch` | No |
| Timestamp earlier than accepted predecessor | `out_of_window` / `timestamp_regression` | No |
| Sequence exhausted | `chain_anomaly` / `sequence_exhausted` | No |
| All checks pass | `valid` | Yes, atomically with event journal |

The first record must be sequence zero with the zero genesis hash. A gap means
missing continuity; it does not prove tampering. A gap record remains quarantined
and can be resubmitted after missing predecessors arrive in order, provided all
are still within the time policy. There is no automatic reanchoring or unsafe
acceptance of an arbitrary initial head. Different validly signed content at an
already-consumed sequence is conservatively a replay; this version does not
separately classify signer equivocation.

Default age tolerance is 120 seconds and future skew is 5 seconds, inclusive at
both boundaries. CLI flags configure each within 0–86400 seconds. These are
prototype operational defaults, not official challenge requirements. Use a UTC
synchronized host. The verifier accepts an injected aware clock for deterministic
experiments. Old authentic records may be ineligible; cryptographic authenticity
alone never overrides freshness or continuity.

## Persistence, failure and recovery

SQLite `BEGIN IMMEDIATE` serializes concurrent verifiers with registry changes.
`synchronous=FULL` commits a successful head update and its verdict together.
Disk/transaction errors fail closed; a test-induced journal failure proves the
head is rolled back. Restarting retains heads, revocations and pending verdicts.
Missing, incompatible or accidentally re-created state must not silently reset
trust. Retain the receiver database and protect it like signer state. A privileged
local attacker or rollback to an old backup is outside software anti-rollback
protection; a hardware/remote monotonic anchor is not claimed.

Once a predecessor is permanently lost or too old, this conservative stream
cannot continue normally. Recover the authenticated history within the chosen
policy, or explicitly provision a new signer stream and enroll it locally.
Preserve the old database/journal and record the administrative recovery. Do not
reset sequence numbers or delete heads to make a demo pass. Automated recovery,
persistent MQTT sessions, reconnection and long-duration reliability remain
Phase 6 work.

## L2/L4 delivery contract

`Verifier.verify(payload, topic=...)` returns `(event_id, VerifiedReading)` and
commits it to the journal. The pipeline sends **every** verdict to the supplied
L4 callback; only `analysis_eligible` records go to an optional L2 callback.
Signed null values remain `valid` provenance but are not analytical input.
Unknown units and physically false signed values remain raw evidence for later
L2/L3 evaluation. A valid signature is not evidence of physical truth.

Callbacks receive `(event_id, verdict)`. Event IDs are stable within one retained
receiver database. Scope deduplication by deployment/database identity when
combining installations. Delivery is at least once: if a consumer crashes after
acting but before acknowledgement, it sees the same ID again. Consumers must be
idempotent. One dispatcher per receiver database is supported; verification may
be concurrent. A failing L4 or L2 callback leaves the event pending. `drain()`
delivers bounded batches of 128 in order. Actual team L4 decisions and L2 signal
processing remain pending; the CLI uses a stdout demonstration sink.

The pending journal bound defaults to 10,000 events; reaching it stops verification
without advancing heads. Delivered events remain as audit history, so disk use
grows over time. Archive/retention, disk monitoring and journal compaction are
future operational work. A storage failure is surfaced rather than dropping
evidence. Malformed events retain a digest, not potentially dangerous raw blobs.

## MQTT limits

The adapter queues at most 128 payloads of at most 65,536 bytes. Network callbacks
do no cryptography or disk I/O. Overflow surfaces as a failure; it is not reported
as a successful complete run. QoS 1 acknowledgement occurs only after durable
verification and successful pipeline delivery. Failed verification/delivery sends
no acknowledgement. The existing broker remains nonpersistent and clients use
clean sessions: acknowledgement discipline is not an end-to-end recovery
promise across reconnects. Start the receiver before publishing. Transport tests
use a real broker, not mocked TLS.

Native Windows execution is still to be confirmed by the user. POSIX tests do
not certify Windows ACL behavior. No public deployment or challenge detection
performance is claimed.
