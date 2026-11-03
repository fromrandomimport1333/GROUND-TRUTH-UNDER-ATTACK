"""Bounded synchronous facade over Paho MQTT v5, requiring mutual TLS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import ssl
import threading
import uuid

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from core.provenance.codec import MAX_PAYLOAD_BYTES
from core.provenance.keys import safe_sensor_id
from core.secure_files import read_private


@dataclass(frozen=True)
class TLSSettings:
    host: str
    port: int
    ca: Path
    certificate: Path
    private_key: Path
    timeout: float = 10.0


def tls_context(settings: TLSSettings) -> ssl.SSLContext:
    if not 1 <= settings.port <= 65535 or not 0 < settings.timeout <= 60:
        raise ValueError("invalid connection bounds")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(settings.ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    read_private(settings.private_key)
    context.load_cert_chain(str(settings.certificate), str(settings.private_key))
    return context


class MQTTConnection:
    def __init__(self, settings: TLSSettings, *, client_id: str | None = None):
        self.settings = settings
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.failure: str | None = None
        self._connection_denied = False
        self.subscription_failure: str | None = None
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or "cis-" + uuid.uuid4().hex, protocol=mqtt.MQTTv5,
            reconnect_on_failure=False)
        self.client.connect_timeout = settings.timeout
        self.client.max_inflight_messages_set(1)
        self.client.max_queued_messages_set(16)
        self.client.tls_set_context(tls_context(settings))
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_publish = self._on_publish
        self._publish_results: dict[int, bool] = {}
        self._result_lock = threading.Lock()
        self._loop_started = False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._connection_denied = True
            self.failure = f"broker refused connection: {reason_code}"
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        if reason_code.is_failure:
            self.failure = f"broker disconnected: {reason_code}"

    def _on_subscribe(self, client, userdata, mid, reasons, properties):
        if any(code.is_failure for code in reasons):
            self.subscription_failure = "broker denied subscription"
        self.subscribed.set()

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        with self._result_lock:
            self._publish_results[mid] = not reason_code.is_failure

    def connect(self) -> None:
        properties = Properties(PacketTypes.CONNECT)
        properties.MaximumPacketSize = MAX_PAYLOAD_BYTES
        properties.SessionExpiryInterval = 0
        try:
            result = self.client.connect(self.settings.host, self.settings.port,
                                         keepalive=30, clean_start=True, properties=properties)
            if result != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError("MQTT connect call failed")
            self.client.loop_start()
            self._loop_started = True
            if not self.connected.wait(self.settings.timeout):
                raise TimeoutError("MQTT CONNACK timed out")
            if self._connection_denied:
                raise PermissionError(self.failure)
            if self.failure:
                raise ConnectionError(self.failure)
        except BaseException:
            self.close()
            raise

    def subscribe(self, topic: str) -> None:
        self.subscribed.clear()
        self.subscription_failure = None
        result, _ = self.client.subscribe(topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS or not self.subscribed.wait(self.settings.timeout):
            raise TimeoutError("MQTT subscription failed or timed out")
        if self.subscription_failure:
            raise PermissionError(self.subscription_failure)

    def publish(self, sensor_id: str, payload: bytes) -> None:
        safe_sensor_id(sensor_id)
        if type(payload) is not bytes or not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
            raise ValueError("invalid publication size/type")
        if not self.client.is_connected() or self.failure:
            raise ConnectionError("MQTT connection unavailable; signed record remains pending")
        properties = Properties(PacketTypes.PUBLISH)
        properties.ContentType = "application/cbor"
        # QoS 1 deliberately allows duplicates after uncertain delivery.
        info = self.client.publish(f"sensors/raw/{sensor_id}", payload, qos=1, retain=False,
                                   properties=properties)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT publication enqueue failed")
        info.wait_for_publish(timeout=self.settings.timeout)
        if not info.is_published():
            raise TimeoutError("MQTT PUBACK timed out; record remains pending")
        with self._result_lock:
            success = self._publish_results.pop(info.mid, False)
        if not success:
            raise PermissionError("broker denied publication or no successful PUBACK was received")

    def close(self) -> None:
        self.client.disconnect()
        if self._loop_started:
            self.client.loop_stop()
            self._loop_started = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()
