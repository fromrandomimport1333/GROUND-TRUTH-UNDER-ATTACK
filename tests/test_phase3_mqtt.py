"""L1b integration against real loopback Mosquitto with existing mTLS ACLs."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from test_phase2_broker import broker, PASSWORD
from adapters.live_mqtt import LiveMQTT
from adapters.mqtt_transport import MQTTConnection
from core.local_config import connection_settings
from core.pipeline import ProvenancePipeline
from core.provenance.codec import encode_reading
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import load_private_key
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier


@pytest.mark.broker
@pytest.mark.integration
def test_live_verdicts_over_tls_and_persistent_replay(broker, tmp_path):
    root, manifest = broker
    registry = KeyRegistry.import_bundle(tmp_path / 'receiver.sqlite3', root / 'sensors')
    state = SignerState.create(tmp_path / 'signer.sqlite3', [
        (s['sensor_id'], s['key_id'], s['stream_id']) for s in manifest['sensors']])
    l4, l2 = [], []
    verifier = Verifier(registry)
    pipeline = ProvenancePipeline(verifier, lambda i,v: l4.append(v), lambda i,v: l2.append(v))
    with LiveMQTT(connection_settings(root, manifest, 'observer'), pipeline) as receiver:
        for item in manifest['sensors']:
            key = load_private_key(root / 'sensors' / item['private_key'], PASSWORD)
            signer = SensorSigner(item['sensor_id'], item['stream_id'], key, state)
            reading = signer.sign(24.0)
            with MQTTConnection(connection_settings(root, manifest, item['sensor_id'])) as sender:
                sender.publish(item['sensor_id'], encode_reading(reading))
                assert receiver.receive(5).status == 'valid'
                sender.publish(item['sensor_id'], encode_reading(reading))
                assert receiver.receive(5).status == 'replay'
                sender.publish(item['sensor_id'], b'invalid-cbor')
                assert receiver.receive(5).status == 'malformed'
    assert len(l4) == 9 and len(l2) == 3
    assert Verifier(KeyRegistry(registry.path)).verify(encode_reading(reading))[1].status == 'replay'


@pytest.mark.broker
@pytest.mark.integration
def test_registry_and_receiver_cli_restart(broker, tmp_path):
    root, manifest = broker
    project = Path(__file__).resolve().parents[1]
    state = tmp_path / 'receiver.sqlite3'
    init = subprocess.run([sys.executable, '-m', 'scripts.manage_registry', '--state', str(state),
                           'init', '--bundle', str(root / 'sensors')], cwd=project,
                          capture_output=True, text=True, timeout=15)
    assert init.returncode == 0, init.stderr
    all_events = []
    for _ in range(2):
        process = subprocess.Popen([sys.executable, '-m', 'scripts.receive_mqtt', '--local', str(root),
            '--state', str(state), '--count', '3', '--timeout', '10'], cwd=project,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        try:
            assert 'VERIFIER_READY' in ready.get(timeout=10)
            sent = subprocess.run([sys.executable, 'sensor.py', '--local', str(root), '--count', '1'],
                cwd=project, env={**os.environ, 'CIS_KEY_PASSWORD': PASSWORD.decode()},
                capture_output=True, text=True, timeout=15)
            assert sent.returncode == 0, sent.stderr
            output, errors = process.communicate(timeout=15)
            assert process.returncode == 0, errors
            events = [json.loads(line) for line in output.splitlines()]
            assert len(events) == 3
            assert all(e['provenance']['status'] == 'valid' for e in events)
            all_events.extend(events)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    assert len({e['event_id'] for e in all_events}) == 6
    assert sorted(e['provenance']['reading']['sequence'] for e in all_events) == [0,0,0,1,1,1]


def test_callback_queue_bounds_and_fail_closed():
    receiver = object.__new__(LiveMQTT)
    receiver.incoming = queue.Queue(maxsize=1)
    receiver.overflow = threading.Event()
    message = SimpleNamespace(topic='sensors/raw/a', payload=b'x', mid=1, qos=1)
    receiver._on_message(None, None, message)
    receiver._on_message(None, None, message)
    assert receiver.incoming.qsize() == 1
    with pytest.raises(BufferError):
        receiver.receive()


def test_no_mqtt_ack_on_verification_failure():
    receiver = object.__new__(LiveMQTT)
    receiver.incoming = queue.Queue()
    receiver.incoming.put(('sensors/raw/a', b'x', 1, 1))
    receiver.overflow = threading.Event()
    acks = []
    connected = threading.Event()
    connected.set()
    receiver.connection = SimpleNamespace(failure=None, connected=connected, client=SimpleNamespace(ack=lambda *a: acks.append(a)))
    def fail(*args, **kwargs):
        raise OSError('storage failed')
    receiver.pipeline = SimpleNamespace(receive=fail)
    with pytest.raises(OSError):
        receiver.receive()
    assert not acks
