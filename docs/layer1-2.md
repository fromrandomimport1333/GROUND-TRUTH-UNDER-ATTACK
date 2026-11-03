# Layers 1 and 2: provenance, transport and signal processing

Operator detail for Layer 1 (Ed25519 signing, MQTT/TLS transport, persistent verification) and
Layer 2 (validation, alignment, feature windows). For the overall system, start at the
[README](../README.md); for the jury test path, see [jury-guide.md](jury-guide.md).

## Run the processing prototype

From the project root, using your environment's Python:

```text
python -m scripts.demo_processing
```

This needs no broker setup and exercises real signatures followed by Phase 4
processing on 48 explicitly synthetic C/F/K observations. It emits a FeatureWindow.
See [processing policies and feature definitions](phase4-processing.md) for
normalization, evidence retention, alignment, spectral prerequisites, reference
availability and API usage. To process your own trusted receiver journal, use
`scripts.build_feature_window` with explicit start/end/as-of timestamps as shown
in the Windows guide.

## Fresh installation

Target CPython 3.14 and Mosquitto 2.1.2. Docker is not required. Downloads:
https://www.python.org/downloads/ and https://mosquitto.org/download/.

Windows PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Linux/macOS:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pytest -q
```

Commands below use `python` as shorthand for your environment's executable.

## Offline signing demo

```text
python -m scripts.demo_signing
```

This uses temporary synthetic keys/readings to demonstrate three independent
sensor chains, CBOR round trips and detection of changed signed bytes. It is
not a real dataset evaluation or a runtime receiver verifier.

## Local MQTT/TLS demo

Provision once at the final project location:

```text
python -m scripts.setup_local
python -m scripts.manage_registry init
```

Enter a password of at least 16 characters. Input is not echoed. Retain it for
loading sensor keys. Provisioning creates a protected `local/` directory with
three encrypted sensor keys, separate TLS identities, a public registry
description, broker config/ACLs and initialized signer state. Existing bundles
are never overwritten. Do not move `local/`: broker paths refer to its location.
See [phase2-security.md](phase2-security.md) for the credential and trust model.

Open three terminals at the project root:

1. Start the broker: `mosquitto -c local/mosquitto.conf`.
2. Start reception: `python -m scripts.receive_mqtt --count 9`.
3. Wait for `VERIFIER_READY`, then run `python sensor.py --count 3` and enter
   the same sensor-key password.

On Windows the broker can be started with:

```powershell
& "C:\Program Files\mosquitto\mosquitto.exe" -c ".\local\mosquitto.conf"
```

The listener binds only 127.0.0.1:8883, requires client certificates and has no
plaintext fallback. Each sensor can publish only to its own
`sensors/raw/sensor_XX` topic. The observer has read-only access to
`sensors/raw/+`. Sensor signatures remain independent of TLS authentication.

Each sensor emits a temperature between 20 and 30 Celsius every two seconds by
default. `--count` is rounds per sensor; zero runs until Ctrl+C. `--seed 42`
repeats synthetic sample values without fixing cryptographic keys. An explicit
`--interval` changes the sampling interval. Dataset input remains an adapter
extension, not an invented CSV format.

The verifier reports `VerifiedReading` events and persists its independent
sequence/chain state in `local/receiver.sqlite3`. Initialize it before the first
publication and retain it across restarts. A new verifier cannot trust an already
advanced signer without the missing history. Use a fresh named bundle (see [windows-phase2.md](windows-phase2.md)) if the simulator has already run. The old `observe_mqtt`
script remains available for diagnostic decoding only.

A valid signature establishes enrolled-key provenance, not physical credibility.
See [phase3-security.md](phase3-security.md) for verdict precedence, gap quarantine, time windows,
key rotation, revocation and the L2/L4 callback interface.

## Restart and delivery semantics

The signer atomically commits sequence allocation, chain advancement and exact
signed CBOR to a bounded outbox before publishing. Successful MQTT QoS 1 PUBACK
removes the matching entry. Failed or uncertain publication stays pending.
Restart with the same bundle retries exact pending bytes and resumes sequences.

This is at-least-once publication to the broker, not exactly-once end-to-end
processing. PUBACK is not proof that L3/L4 received a record. The local broker
has no disk message persistence; the observer uses a clean session. Start the
observer before a finite demonstration. Duplicates after uncertain delivery
are expected and classified by the persistent Phase 3 receiver. Automatic reconnection and
prolonged stability are later reliability work.

Do not delete `signer.sqlite3` to reset a sensor. Missing state fails closed.
New streams require explicit provisioning and receiver authorization. Clock
regression stops signing instead of resetting state. A malicious local
administrator or old database backup can defeat a purely software monotonic
anchor; no hardware anti-rollback claim is made.

## Testing

```text
python -m pytest -q
```

The default suite explicitly skips real broker tests. Include actual loopback
MQTT/TLS tests using:

```text
python -m pytest -q --run-broker --broker-executable /absolute/path/to/mosquitto
```

On Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --run-broker --broker-executable "C:\Program Files\mosquitto\mosquitto.exe"
```

Tests create temporary certificates and a separate broker on an ephemeral
loopback port, then stop it. They do not change an existing Windows service.
An explicitly requested missing broker fails. A POSIX symlink test is skipped
on Windows because symlink creation can require extra privileges; Windows ACL
provisioning is exercised when the remaining provisioning tests run there.

## Source layout

| Path | Responsibility |
| --- | --- |
| `core/contracts.py` | Versioned immutable interfaces, unchanged from Phase 1 |
| `core/provenance/codec.py` | Signing bytes, chain digest and bounded canonical CBOR |
| `core/provenance/keys.py` | Encrypted sensor-key generation/loading |
| `core/provenance/pki.py` | Local development CA and TLS identities |
| `core/provenance/state.py` | Atomic sequence/chain state and durable outbox |
| `core/provenance/signer.py` | Layer 1a signing |
| `adapters/mqtt_transport.py` | Required mutual TLS and bounded MQTT v5 calls |
| `core/provenance/keyregistry.py` | Explicit trust enrollment, revocation and rotation |
| `core/provenance/verifier.py` | Persistent signature, replay and chain verification |
| `core/provenance/replay_protection.py` | Configurable age/future-skew policy |
| `core/pipeline.py` | Durable at-least-once L4/L2 routing seam |
| `adapters/live_mqtt.py` | Bounded receiver with manual QoS acknowledgement |
| `scripts/manage_registry.py`, `scripts/receive_mqtt.py` | Receiver administration and CLI |
| `sensor.py` | Independent multi-sensor simulation |
| `generate_keys.py` | Standalone encrypted sensor-key provisioning |
| `scripts/setup_local.py` | Complete private local bundle |
| `scripts/observe_mqtt.py` | Diagnostic subscriber, not Layer 1b |
| `core/processing/validator.py`, `engine.py` | Semantic validation, normalization and bounded FeatureWindow construction |
| `core/processing/alignment.py`, `features.py` | UTC buckets and temporal/spectral/spatial features |
| `core/processing/references.py`, `policy.py` | Causal reference matching and explicit processing policy |
| `core/processing/journal.py` | Read complete trusted verifier evidence without changing it |
| `scripts/demo_processing.py`, `scripts/build_feature_window.py` | Synthetic integrated demo and journal-to-window CLI |
| `attacks/`, `eval/` | Attack scenarios and evaluation (see [project-structure.md](project-structure.md)) |

## Reproducibility and integration

Install the checked-in, hash-locked `requirements.txt`; do not regenerate it for
routine execution. It includes platform markers and was generated with:

```text
uv pip compile requirements.in --universal --python-version 3.14 --generate-hashes -o requirements.txt
```

The CBOR minimum reflects the decoder-hardening API used by Phase 2. Hashes
constrain package artifacts but are not a dependency vulnerability audit.
Fixed v1 protocol bytes and a deliberately public RFC 8032 test seed are in
`tests/vectors/reading_v1.json`. Never deploy that public test key.

An optional editable install after locked dependencies is
`python -m pip install --no-deps --no-build-isolation -e .`. Running from the
project root requires no package installation.

Further reading: [contracts.md](contracts.md), [phase2-security.md](phase2-security.md),
[phase3-security.md](phase3-security.md), [phase4-processing.md](phase4-processing.md) and
[phase6-performance-reliability.md](phase6-performance-reliability.md).
