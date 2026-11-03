"""Real loopback-only Mosquitto integration. Opt in with --run-broker."""

from dataclasses import replace
from pathlib import Path
import json
import os
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time

from cryptography.hazmat.primitives import serialization
import pytest

from adapters.mqtt_transport import MQTTConnection
from core.local_config import connection_settings, load_manifest
from core.provenance.codec import decode_reading, encode_reading, signing_bytes
from core.provenance.keys import load_private_key
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from scripts.setup_local import setup_local

pytestmark = [pytest.mark.integration, pytest.mark.broker]
PASSWORD = b"broker-integration-test-only-password"


@pytest.fixture(scope="module")
def broker(request, tmp_path_factory):
    if not request.config.getoption("--run-broker"):
        pytest.skip("real broker tests require --run-broker")
    executable = request.config.getoption("--broker-executable") or shutil.which("mosquitto")
    if not executable or not Path(executable).is_file():
        pytest.fail("--run-broker requested but Mosquitto executable was not found")
    # Select an ephemeral loopback port. A bind race causes a visible test failure.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = setup_local(tmp_path_factory.mktemp("broker-tests") / "local bundle", PASSWORD, port=port)
    log = (root / "broker.log").open("wb")
    process = subprocess.Popen([str(executable), "-c", str(root / "mosquitto.conf")],
                               stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                pytest.fail("broker exited before startup: " + (root / "broker.log").read_text(errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("broker startup timed out")
                time.sleep(0.05)
        yield root, load_manifest(root)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def test_three_sensors_publish_actual_signed_cbor_over_mtls(broker):
    root, manifest = broker
    incoming = queue.Queue()
    observer = MQTTConnection(connection_settings(root, manifest, "observer"))
    observer.client.on_message = lambda client, userdata, msg: incoming.put((msg.topic, msg.payload))
    state = SignerState(root / "signer.sqlite3")
    with observer:
        observer.subscribe("sensors/raw/+")
        for item in manifest["sensors"]:
            key = load_private_key(root / "sensors" / item["private_key"], PASSWORD)
            signer = SensorSigner(item["sensor_id"], item["stream_id"], key, state)
            reading = signer.sign(25.0)
            with MQTTConnection(connection_settings(root, manifest, item["sensor_id"])) as publisher:
                publisher.publish(reading.sensor_id, encode_reading(reading))
            topic, payload = incoming.get(timeout=5)
            restored = decode_reading(payload)
            assert restored == reading
            assert topic == f"sensors/raw/{item['sensor_id']}"
            key.public_key().verify(restored.signature, signing_bytes(restored))
            state.acknowledge(restored)


def test_sensor_cannot_publish_to_another_sensors_topic(broker):
    root, manifest = broker
    with MQTTConnection(connection_settings(root, manifest, "sensor_01")) as publisher:
        with pytest.raises((PermissionError, ConnectionError, TimeoutError)):
            publisher.publish("sensor_02", b"forbidden-test")


def test_observer_cannot_publish(broker):
    root, manifest = broker
    with MQTTConnection(connection_settings(root, manifest, "observer")) as observer:
        with pytest.raises((PermissionError, ConnectionError, TimeoutError)):
            observer.publish("sensor_01", b"forbidden-test")


def test_untrusted_server_ca_is_rejected(broker, tmp_path):
    root, manifest = broker
    other = setup_local(tmp_path / "other", PASSWORD)
    settings = replace(connection_settings(root, manifest, "sensor_01"), ca=other / "tls/ca.pem")
    with pytest.raises(ssl.SSLCertVerificationError):
        with MQTTConnection(settings):
            pytest.fail("untrusted server was accepted")


def test_wrong_hostname_is_rejected(broker, monkeypatch):
    root, manifest = broker
    settings = replace(connection_settings(root, manifest, "sensor_01"), host="wrong.example")
    original = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs:
                        original("127.0.0.1" if host == "wrong.example" else host, *args, **kwargs))
    with pytest.raises(ssl.SSLCertVerificationError):
        with MQTTConnection(settings):
            pytest.fail("wrong server hostname was accepted")


def test_tls_client_without_certificate_is_rejected(broker):
    root, manifest = broker
    context = ssl.create_default_context(cafile=str(root / "tls/ca.pem"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # Pin 1.2 only in this negative test so rejection occurs during handshake.
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(("127.0.0.1", manifest["port"]), timeout=3) as raw:
        with pytest.raises(ssl.SSLError):
            context.wrap_socket(raw, server_hostname="localhost")


def test_untrusted_client_certificate_is_rejected(broker, tmp_path):
    root, manifest = broker
    other = setup_local(tmp_path / "untrusted-client", PASSWORD)
    settings = replace(connection_settings(root, manifest, "sensor_01"),
                       certificate=other / "tls/sensor_01.pem",
                       private_key=other / "tls/sensor_01.key.pem", timeout=1.0)
    with pytest.raises((ssl.SSLError, ConnectionError, TimeoutError)):
        with MQTTConnection(settings):
            pytest.fail("untrusted client certificate was accepted")


def test_real_simulator_and_observer_cli_and_restart(broker):
    root, manifest = broker
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "CIS_KEY_PASSWORD": PASSWORD.decode()}

    def run_rounds(rounds):
        observer = subprocess.Popen([sys.executable, "-m", "scripts.observe_mqtt", "--local", str(root),
                                     "--count", str(rounds * 3), "--timeout", "15"], cwd=project,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_line = queue.Queue()
        reader = threading.Thread(target=lambda: first_line.put(observer.stdout.readline()), daemon=True)
        reader.start()
        try:
            assert "OBSERVER_READY" in first_line.get(timeout=10)
            sent = subprocess.run([sys.executable, "sensor.py", "--local", str(root), "--count", str(rounds),
                                   "--interval", "0.05", "--seed", "42"], cwd=project, env=environment,
                                  capture_output=True, text=True, timeout=15)
            assert sent.returncode == 0, sent.stderr
            received, errors = observer.communicate(timeout=15)
            assert observer.returncode == 0, errors
            events = [json.loads(line) for line in received.splitlines() if line.startswith("{")]
            assert len(events) == rounds * 3
            assert all(event["event"] == "RECEIVED_CBOR" for event in events)
            assert all(20.0 <= event["value"] <= 30.0 for event in events)
            return events
        finally:
            if observer.poll() is None:
                observer.terminate()
                observer.wait(timeout=5)

    first = run_rounds(3)
    second = run_rounds(1)
    for sensor in ("sensor_01", "sensor_02", "sensor_03"):
        sequences = [event["sequence"] for event in first if event["sensor_id"] == sensor]
        assert sequences == list(range(sequences[0], sequences[0] + 3))
        assert next(event["sequence"] for event in second if event["sensor_id"] == sensor) == sequences[-1] + 1
