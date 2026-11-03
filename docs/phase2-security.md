> Historical Phase 2 design notes. Phase 3 receiver policies are now implemented;
> see `phase3-security.md` for current registry, replay and recovery behavior.

# Phase 2 security and protocol decisions

## Signed bytes and chain digest

Version 1 now implements the Phase 1 serialization proposal exactly:

* Signing input is `b"CIS/reading/v1\0"` plus canonical CBOR of all SignedReading
  fields except `signature`.
* The chain digest is SHA-256 of `b"CIS/chain/v1\0"` plus canonical CBOR of the
  complete signed envelope, including the signature.
* Genesis `prev_hash` is 32 zero bytes. Each provisioned sensor/key/stream starts
  at sequence 0. Explicit defaults such as null location remain in the map.
* Timestamps are exact UTC strings with six fractional digits. Wire hash and
  signature fields are CBOR bytes; diagnostic JSON uses lowercase hex.

`tests/vectors/reading_v1.json` fixes the bytes, signature and digest for a
public RFC 8032 test seed. The seed is intentionally public and must never be a
deployment key. Normal provisioning always uses fresh cryptographic randomness.

The decoder bounds total bytes, nesting, map sizes and string declarations
before general decoding. The v1 subset rejects arrays, semantic tags, indefinite
containers, duplicate keys, trailing objects and noncanonical encodings. Contract
validation rejects unsupported versions, non-finite values and incorrect types.
Decoding is not signature verification and does not yield a VerifiedReading.

## Local credentials and trust boundaries

Ed25519 sensor keys are encrypted PKCS8. TLS server/client keys are distinct
ECDSA keys, stored unencrypted so Mosquitto/Paho can load them unattended.
Restrictive per-user permissions protect those TLS keys; the CA private key is
encrypted. POSIX uses 0700 directories/0600 files. Windows provisioning removes
inherited ACLs and grants the current account full access on new private paths.
This is a single-account laptop demo, not isolation among hostile local users
running as the same account. Local administrators and host compromise are outside
the trust anchor. Python does not guarantee secret zeroization.

New writes use exclusive creation and reject symlink paths. Existing bundles
are never overwritten. Setup failures may leave an incomplete bundle; do not
reuse it as a completed identity set. Review the failure and choose a fresh
output directory. No executable silently deletes credentials or resets state.

The development CA is the TLS trust root. Certificates include basic constraints,
key usage, extended key usage, subject/authority key identifiers, bounded validity,
and server SANs for localhost and 127.0.0.1. Python verifies hostname and trust
with a TLS 1.2 minimum; no insecure fallback exists. Certificate identifiers
were added after actual Python 3.14 strict-validation tests rejected the initial
certificates. Verification was kept enabled throughout the fix.

The broker binds 127.0.0.1 only, requires certificates and disallows anonymous
access. Sensor certificate CNs select ACL identities. Each sensor can write only
its own `sensors/raw/sensor_XX` topic; the observer can only read `sensors/raw/+`.
This per-sensor topic subdivision makes the team's topic boundary enforceable.
On POSIX the foreground broker explicitly uses the provisioning account so it
can read that account's protected keys. Do not deploy the generated local config
as a root-run public service.

Application signatures are independent of TLS identity. A compromised sensor can
still sign false values. Cryptographic authenticity never establishes physical
credibility; detection belongs to L2 features and L3. Full registry verification,
revocation, rotation and trusted stream admission are Phase 3.

## Durable signing and publication

SQLite uses immediate transactions and FULL synchronization. Each transaction
allocates a sequence, signs the reading, inserts exact CBOR into a bounded outbox,
and advances the chain head. A failed validation/transaction rolls back allocation.
Concurrent signers serialize through SQLite. The outbox defaults to 128 pending
records per sensor; saturation stops signing rather than discarding evidence.

A successful QoS 1 PUBACK removes only the matching pending bytes. ACL rejection,
timeout or connection failure retains them. Restart retries exact pending records
before producing new readings. Duplicate receipts after uncertain acknowledgements
are expected. Pending old timestamps are preserved for later freshness policy.

This guarantees recoverable publication attempts, not exactly-once or end-to-end
delivery. A PUBACK says the broker accepted publication, not that L3 processed it.
The local broker uses no disk message persistence and the diagnostic observer uses
a clean session. Start the observer first for the finite demo. Pipeline consumer
acknowledgements and prolonged disconnect/reconnect behavior are later integration
and reliability work. Automatic reconnection is not implemented in Phase 2.

Missing state, unknown streams, backward clocks and sequence exhaustion fail
closed. Replacing a database with an older backup can roll back software state;
there is no hardware monotonic anchor. Protect backups and retain the same
provisioned bundle. Receiver-side state checks must address duplicate or rollback
evidence in Phase 3. `stream_id` is not self-authorizing.

## Remaining boundaries

`config/development.json` retains inactive Phase 1 design defaults. Phase 2
runtime reads the generated `local/manifest.json` and `local/mosquitto.conf`;
editing the inactive design file does not reconfigure the live transport.

No runtime Layer 1b verifier, replay classifier, registry policy, anomaly detector,
Layer 2 normalization/features, L4 routing or benchmark is claimed here; those
came in later phases. No challenge detection score is inferred from these tests.
