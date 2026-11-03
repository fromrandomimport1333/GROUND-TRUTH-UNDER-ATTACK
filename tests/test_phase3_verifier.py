"""Synthetic cryptographic and persistent-state tests, not dataset evaluation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core.contracts import SignedReading
from core.pipeline import ProvenancePipeline
from core.provenance.codec import encode_reading, signing_bytes
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import key_id
from core.provenance.replay_protection import ReplayPolicy
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@pytest.fixture
def rig(tmp_path):
    key = Ed25519PrivateKey.generate()
    registry = KeyRegistry.create(tmp_path / 'receiver.sqlite3')
    registry.enroll('sensor_01', key.public_key(), 'stream_01')
    state = SignerState.create(tmp_path / 'signer.sqlite3', [('sensor_01', key_id(key), 'stream_01')])
    signer = SensorSigner('sensor_01', 'stream_01', key, state)
    verifier = Verifier(registry, clock=lambda: NOW)
    return key, registry, signer, verifier


def changed(reading, key, **updates):
    result = SignedReading(**{**reading.model_dump(), **updates})
    return SignedReading(**{**result.model_dump(), 'signature': key.sign(signing_bytes(result))})


def check(verifier, reading, **kwargs):
    return verifier.verify(encode_reading(reading), **kwargs)[1]


def test_valid_and_durable_replay(rig):
    key, registry, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, reading).status == 'valid'
    restarted = Verifier(KeyRegistry(registry.path), clock=lambda: NOW)
    assert check(restarted, reading).status == 'replay'
    assert [v.status for _, v in restarted.pending()] == ['valid', 'replay']


@pytest.mark.parametrize('field,value', [('value', 99.0), ('unit', 'K'), ('sequence', 99),
    ('prev_hash', b'x'*32), ('timestamp', '2026-10-20T12:00:01.000000Z'), ('quantity', 'humidity')])
def test_tampering_does_not_poison_head(rig, field, value):
    _, _, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    tampered = SignedReading(**{**reading.model_dump(), field: value})
    assert check(verifier, tampered).status == 'bad_signature'
    assert check(verifier, reading).status == 'valid'


@pytest.mark.parametrize('field,value', [('sensor_id', 'intruder'), ('key_id', 'unregistered'), ('stream_id', 'reset')])
def test_unknown_enrollment(rig, field, value):
    key, _, signer, verifier = rig
    reading = changed(signer.sign(24.0, timestamp=NOW), key, **{field: value})
    verdict = check(verifier, reading)
    assert verdict.status == 'unknown_sensor'
    assert verdict.signature_verified is None


def test_revocation_and_atomic_rotation(rig):
    key, registry, signer, verifier = rig
    old = signer.sign(24.0, timestamp=NOW)
    replacement = Ed25519PrivateKey.generate()
    registry.enroll('sensor_01', replacement.public_key(), 'stream_02', retire_key=key_id(key))
    assert check(verifier, old).status == 'revoked_key'
    new = changed(old, replacement, key_id=key_id(replacement), stream_id='stream_02')
    assert check(verifier, new).status == 'valid'
    with pytest.raises(ValueError):
        registry.enroll('sensor_01', key.public_key(), 'another')
    with pytest.raises(ValueError):
        registry.revoke('missing', key_id(key))


def test_duplicate_enrollment_cannot_reset_and_failed_rotation_rolls_back(rig):
    key, registry, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, reading).status == 'valid'
    with pytest.raises(sqlite3.IntegrityError):
        registry.enroll('sensor_01', key.public_key(), 'stream_01')
    with pytest.raises(ValueError):
        registry.enroll('sensor_01', Ed25519PrivateKey.generate().public_key(), 'new', retire_key='missing')
    assert check(verifier, reading).status == 'replay'


def test_gap_and_reordering_recover_only_with_missing_records(rig):
    _, _, signer, verifier = rig
    first, second, third = [signer.sign(float(i), timestamp=NOW) for i in range(3)]
    assert check(verifier, third).status == 'gap'
    assert check(verifier, first).status == 'valid'
    assert check(verifier, third).status == 'gap'
    assert check(verifier, second).status == 'valid'
    assert check(verifier, third).status == 'valid'
    assert check(verifier, second).status == 'replay'


def test_genesis_and_wrong_chain_do_not_advance(rig):
    key, _, signer, verifier = rig
    first = signer.sign(25.0, timestamp=NOW)
    assert check(verifier, changed(first, key, prev_hash=b'x'*32)).status == 'chain_anomaly'
    assert check(verifier, first).status == 'valid'
    second = signer.sign(25.0, timestamp=NOW)
    assert check(verifier, changed(second, key, prev_hash=bytes(32))).status == 'chain_anomaly'
    assert check(verifier, second).status == 'valid'


@pytest.mark.parametrize('seconds,status', [(-121,'out_of_window'), (-120,'valid'), (5,'valid'), (6,'out_of_window')])
def test_time_boundaries(rig, seconds, status):
    _, _, signer, verifier = rig
    assert check(verifier, signer.sign(24.0, timestamp=NOW+timedelta(seconds=seconds))).status == status


def test_timestamp_regression_and_future_retry(rig):
    key, _, signer, verifier = rig
    first = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, first).status == 'valid'
    second = signer.sign(24.0, timestamp=NOW+timedelta(seconds=10))
    assert check(verifier, second).status == 'out_of_window'
    regression = changed(second, key, timestamp='2026-10-20T11:59:59.000000Z')
    assert check(verifier, regression).findings == ('timestamp_regression',)
    verifier.clock = lambda: NOW + timedelta(seconds=10)
    assert check(verifier, second).status == 'valid'


def test_duplicate_before_age_classification(rig):
    _, _, signer, verifier = rig
    first = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, first).status == 'valid'
    verifier.clock = lambda: NOW + timedelta(hours=1)
    assert check(verifier, first).status == 'replay'


def test_topic_binding_no_advance(rig):
    _, _, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    verdict = check(verifier, reading, topic='sensors/raw/sensor_02')
    assert verdict.status == 'chain_anomaly'
    assert verdict.findings == ('topic_identity_mismatch',)
    assert check(verifier, reading, topic='sensors/raw/sensor_01').status == 'valid'


@pytest.mark.parametrize('payload', [b'', b'junk', b'\xa0', b'x'*65537, b'\xbf\xff'])
def test_malformed_is_durable_evidence(rig, payload):
    import hashlib
    verifier = rig[3]
    _, result = verifier.verify(payload)
    assert result.status == 'malformed' and result.reading is None
    assert result.raw_message_sha256 == hashlib.sha256(payload).digest()
    assert verifier.pending()[0][1] == result


def test_concurrent_duplicate_exactly_one_valid(rig):
    _, _, signer, verifier = rig
    raw = encode_reading(signer.sign(24.0, timestamp=NOW))
    with ThreadPoolExecutor(max_workers=8) as pool:
        verdicts = list(pool.map(lambda _: verifier.verify(raw)[1].status, range(16)))
    assert verdicts.count('valid') == 1
    assert verdicts.count('replay') == 15


def test_failure_to_persist_event_rolls_back_head(rig):
    _, registry, signer, verifier = rig
    with registry.transaction() as db:
        db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'test storage failure'); END")
    reading = signer.sign(24.0, timestamp=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        check(verifier, reading)
    with registry.transaction() as db:
        db.execute('DROP TRIGGER fail_event')
    assert check(verifier, reading).status == 'valid'


def test_pipeline_routes_all_events_l4_and_only_eligible_l2(rig):
    key, _, signer, verifier = rig
    l4, l2 = [], []
    pipeline = ProvenancePipeline(verifier, lambda i,v: l4.append((i,v)), lambda i,v: l2.append((i,v)))
    reading = signer.sign(99.0, timestamp=NOW)  # Valid signatures do not certify physical truth.
    assert pipeline.receive(encode_reading(reading)).status == 'valid'
    pipeline.receive(encode_reading(reading))
    pipeline.receive(b'invalid')
    missing = signer.sign(None, timestamp=NOW)
    assert pipeline.receive(encode_reading(missing)).status == 'valid'
    assert [v.status for _,v in l4] == ['valid','replay','malformed','valid']
    assert len(l2) == 1 and l2[0][1].reading.value == 99.0
    assert not verifier.pending()


def test_consumer_failure_and_restart_at_least_once(rig):
    _, registry, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    def fail(*args):
        raise RuntimeError('consumer unavailable')
    with pytest.raises(RuntimeError):
        ProvenancePipeline(verifier, fail).receive(encode_reading(reading))
    event_id, verdict = verifier.pending()[0]
    seen = []
    restarted = Verifier(KeyRegistry(registry.path), clock=lambda: NOW)
    assert ProvenancePipeline(restarted, lambda i,v: seen.append((i,v))).drain() == 1
    assert seen == [(event_id, verdict)]
    assert check(restarted, reading).status == 'replay'


def test_pending_bound_does_not_advance(rig):
    _, _, signer, verifier = rig
    verifier.max_pending = 1
    first = signer.sign(24.0, timestamp=NOW)
    event_id, _ = verifier.verify(encode_reading(first))
    second = signer.sign(25.0, timestamp=NOW)
    with pytest.raises(BufferError):
        check(verifier, second)
    verifier.acknowledge(event_id)
    assert check(verifier, second).status == 'valid'


def test_missing_state_never_recreated(tmp_path):
    path = tmp_path / 'missing.sqlite3'
    with pytest.raises(FileNotFoundError):
        KeyRegistry(path)
    assert not path.exists()


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), 86401, True])
def test_invalid_time_policy(value):
    with pytest.raises(ValueError):
        ReplayPolicy(max_age_seconds=value)


def test_naive_clock_rejected(rig):
    verifier = rig[3]
    verifier.clock = lambda: datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        verifier.verify(b'invalid')


def test_independent_keys_and_streams(rig):
    key, registry, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, reading).status == 'valid'
    registry.enroll('sensor_01', key.public_key(), 'stream_02')
    assert check(verifier, changed(reading, key, stream_id='stream_02')).status == 'valid'
    assert check(verifier, reading).status == 'replay'
    registry.revoke('sensor_01', key_id(key))
    assert check(verifier, changed(reading, key, stream_id='stream_02')).status == 'revoked_key'


def test_wrong_key_signature_for_enrolled_identity(rig):
    _, _, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, changed(reading, Ed25519PrivateKey.generate())).status == 'bad_signature'
    assert check(verifier, reading).status == 'valid'


def test_layer2_failure_retries_same_event_id(rig):
    _, _, signer, verifier = rig
    l4, l2 = [], []
    def fail(*args):
        raise RuntimeError('layer2 unavailable')
    pipeline = ProvenancePipeline(verifier, lambda i,v: l4.append(i), fail)
    with pytest.raises(RuntimeError):
        pipeline.receive(encode_reading(signer.sign(24.0, timestamp=NOW)))
    pipeline.layer2 = lambda i,v: l2.append(i)
    assert pipeline.drain() == 1
    assert l4 == [l2[0], l2[0]]
    assert not verifier.pending()


def test_registry_import_validates_fingerprint_before_creating_database(tmp_path):
    import json
    from generate_keys import generate_sensor_bundle
    bundle = tmp_path / 'bundle'
    generate_sensor_bundle(bundle, ['sensor_01'], b'synthetic-test-password')
    path = bundle / 'public_registry.json'
    data = json.loads(path.read_text())
    data['sensors'][0]['key_id'] = 'wrong'
    path.write_text(json.dumps(data))
    target = tmp_path / 'receiver.sqlite3'
    with pytest.raises(ValueError, match='fingerprint'):
        KeyRegistry.import_bundle(target, bundle)
    assert not target.exists()


def test_registry_rejects_non_ed25519_and_invalid_identifiers(rig):
    from cryptography.hazmat.primitives.asymmetric import ec
    registry = rig[1]
    with pytest.raises(TypeError):
        registry.enroll('sensor_02', ec.generate_private_key(ec.SECP256R1()).public_key(), 's')
    with pytest.raises(ValueError):
        registry.enroll('../escape', rig[0].public_key(), 's')


def test_existing_state_cannot_be_overwritten(rig):
    key, registry, signer, verifier = rig
    reading = signer.sign(24.0, timestamp=NOW)
    assert check(verifier, reading).status == 'valid'
    with pytest.raises(FileExistsError):
        KeyRegistry.create(registry.path)
    assert check(verifier, reading).status == 'replay'


def test_unsupported_state_version_fails_closed(rig):
    registry = rig[1]
    with registry.transaction() as db:
        db.execute('PRAGMA user_version=99')
    with pytest.raises(ValueError, match='version'):
        KeyRegistry(registry.path)
