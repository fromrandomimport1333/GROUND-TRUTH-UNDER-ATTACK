from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core.contracts import SignedReading
from core.provenance.codec import GENESIS_HASH, decode_reading, encode_reading, record_hash, signing_bytes
from core.provenance.keys import generate_key_pair, key_id, load_private_key
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.secure_files import new_private_directory
from generate_keys import generate_sensor_bundle
from sensor import flush_pending

STAMP = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
def signer_bundle(tmp_path):
    key = Ed25519PrivateKey.generate()
    state = SignerState.create(tmp_path / "signer.sqlite3", [("sensor_01", key_id(key), "stream_01")])
    return SensorSigner("sensor_01", "stream_01", key, state), key, state


def test_signature_and_genesis(signer_bundle):
    signer, key, _ = signer_bundle
    reading = signer.sign(25.5, timestamp=STAMP)
    assert reading.sequence == 0 and reading.prev_hash == GENESIS_HASH
    assert reading.timestamp == "2026-10-20T12:00:00.000000Z"
    key.public_key().verify(reading.signature, signing_bytes(reading))


@pytest.mark.parametrize("changes", [
    {"sensor_id": "sensor_02"}, {"key_id": "other_key"}, {"stream_id": "stream_02"},
    {"timestamp": "2026-10-20T12:00:01.000000Z"}, {"sequence": 1}, {"value": 999.0},
    {"unit": "F"}, {"quantity": "pressure"}, {"prev_hash": bytes.fromhex("ff" * 32)},
    {"location": {"latitude": 36.0, "longitude": 10.0}},
])
def test_every_payload_field_is_signed(signer_bundle, changes):
    signer, key, _ = signer_bundle
    original = signer.sign(25.5, timestamp=STAMP)
    modified = SignedReading.model_validate({**original.model_dump(), **changes})
    with pytest.raises(InvalidSignature):
        key.public_key().verify(modified.signature, signing_bytes(modified))


def test_restarted_signer_resumes_sequence_and_chain(signer_bundle):
    signer, key, state = signer_bundle
    first = signer.sign(25.0, timestamp=STAMP)
    restarted = SensorSigner("sensor_01", "stream_01", key, SignerState(state.path))
    second = restarted.sign(26.0, timestamp=STAMP + timedelta(seconds=2))
    assert second.sequence == 1 and second.prev_hash == record_hash(first)
    assert restarted.state.pending(restarted.identity) == (first, second)


def test_older_record_modification_breaks_hash_continuity(signer_bundle):
    signer, _, _ = signer_bundle
    first = signer.sign(25.0, timestamp=STAMP)
    second = signer.sign(26.0, timestamp=STAMP + timedelta(seconds=2))
    modified = SignedReading.model_validate({**first.model_dump(), "value": 99.0})
    assert record_hash(modified) != second.prev_hash
    changed_signature = SignedReading.model_validate({**first.model_dump(), "signature": bytes(64)})
    assert record_hash(changed_signature) != record_hash(first)


def test_three_sensors_have_independent_keys_sequences_and_chains(tmp_path):
    keys = [Ed25519PrivateKey.generate() for _ in range(3)]
    ids = [(f"sensor_{i + 1:02d}", key_id(k), "stream_01") for i, k in enumerate(keys)]
    state = SignerState.create(tmp_path / "state.sqlite3", ids)
    readings = [SensorSigner(identity[0], identity[2], key, state).sign(25.0, timestamp=STAMP)
                for identity, key in zip(ids, keys)]
    assert len({r.signature for r in readings}) == 3
    assert len({r.key_id for r in readings}) == 3
    assert all(r.sequence == 0 and r.prev_hash == GENESIS_HASH for r in readings)


def test_concurrent_signing_serializes_allocations(signer_bundle):
    signer, _, state = signer_bundle
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: signer.sign(25.0, timestamp=STAMP), range(12)))
    ordered = sorted(results, key=lambda r: r.sequence)
    assert [r.sequence for r in ordered] == list(range(12))
    assert all(b.prev_hash == record_hash(a) for a, b in zip(ordered, ordered[1:]))
    assert len(state.pending(signer.identity)) == 12


def test_invalid_measurement_rolls_back_state(signer_bundle):
    signer, _, state = signer_bundle
    with pytest.raises(ValueError):
        signer.sign(float("nan"), timestamp=STAMP)
    assert state.pending(signer.identity) == ()
    assert signer.sign(25.0, timestamp=STAMP).sequence == 0


def test_clock_regression_does_not_advance_state(signer_bundle):
    signer, _, state = signer_bundle
    first = signer.sign(25.0, timestamp=STAMP)
    with pytest.raises(ValueError, match="backwards"):
        signer.sign(26.0, timestamp=STAMP - timedelta(seconds=1))
    assert state.pending(signer.identity) == (first,)
    assert signer.sign(26.0, timestamp=STAMP).sequence == 1


def test_outbox_backpressure_preserves_chain(signer_bundle):
    signer, _, state = signer_bundle
    first = signer.sign(25.0, timestamp=STAMP)
    with pytest.raises(BufferError):
        state.issue(signer.identity, lambda *_: first, max_pending=1)
    state.acknowledge(first)
    second = signer.sign(26.0, timestamp=STAMP)
    assert second.sequence == 1 and second.prev_hash == record_hash(first)


def test_missing_state_does_not_silently_reset(tmp_path):
    with pytest.raises(FileNotFoundError):
        SignerState(tmp_path / "missing.sqlite3")


def test_new_stream_not_self_authorizing(signer_bundle):
    signer, key, state = signer_bundle
    unprovisioned = SensorSigner("sensor_01", "other_stream", key, state)
    with pytest.raises(ValueError, match="unprovisioned"):
        unprovisioned.sign(25.0, timestamp=STAMP)
    assert state.pending(signer.identity) == ()


def test_pending_record_survives_failed_network_delivery(signer_bundle):
    signer, _, state = signer_bundle
    original = signer.sign(25.0, timestamp=STAMP)

    class FailingConnection:
        def publish(self, *_):
            raise TimeoutError("simulated missing PUBACK")

    with pytest.raises(TimeoutError):
        flush_pending(signer, FailingConnection())
    assert state.pending(signer.identity) == (original,)
    sent = []

    class RecoveredConnection:
        def publish(self, sensor, payload):
            sent.append((sensor, payload))

    assert flush_pending(signer, RecoveredConnection()) == 1
    assert sent == [(original.sensor_id, encode_reading(original))]
    assert state.pending(signer.identity) == ()


def test_acknowledging_changed_payload_does_not_drop_original(signer_bundle):
    signer, _, state = signer_bundle
    original = signer.sign(25.0, timestamp=STAMP)
    changed = SignedReading.model_validate({**original.model_dump(), "value": 99.0})
    state.acknowledge(changed)
    assert state.pending(signer.identity) == (original,)


def test_sequence_exhaustion_fails_without_creating_record(signer_bundle):
    signer, _, state = signer_bundle
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE streams SET next_sequence=?", (2**63 - 1,))
    with pytest.raises(OverflowError):
        signer.sign(25.0, timestamp=STAMP)
    assert state.pending(signer.identity) == ()


def test_encrypted_keys_and_no_overwrite(tmp_path):
    directory = new_private_directory(tmp_path / "sensors")
    password = b"test-only-password-at-least-16"
    info = generate_key_pair(directory, "sensor_01", password)
    private = directory / info["private_key"]
    original = private.read_bytes()
    assert b"ENCRYPTED PRIVATE KEY" in original
    loaded = load_private_key(private, password)
    assert key_id(loaded) == info["key_id"]
    with pytest.raises(ValueError):
        load_private_key(private, b"wrong-password-long-enough")
    with pytest.raises(FileExistsError):
        generate_key_pair(directory, "sensor_01", password)
    assert private.read_bytes() == original


@pytest.mark.parametrize("bad_id", ["../sensor", "sensor:01", "sensor/01", "", "a" * 49])
def test_key_generation_rejects_unsafe_identifiers(tmp_path, bad_id):
    with pytest.raises(ValueError):
        generate_sensor_bundle(tmp_path / "new", [bad_id], b"long-test-only-password")
    assert not (tmp_path / "new").exists()


def test_public_registry_has_no_private_key_paths(tmp_path):
    generate_sensor_bundle(tmp_path / "sensors", ["sensor_01"], b"long-test-only-password")
    assert "private_key" not in (tmp_path / "sensors/public_registry.json").read_text()


def test_naive_timestamps_rejected(signer_bundle):
    signer, _, _ = signer_bundle
    with pytest.raises(ValueError):
        signer.sign(25.0, timestamp=datetime(2026, 1, 1))
