"""Bounded MQTT v5 TLS adapter with manual PUBACK after durable verification."""
import queue
import threading
import time
import math

import paho.mqtt.client as mqtt

from adapters.mqtt_transport import MQTTConnection, TLSSettings
from core.pipeline import ProvenancePipeline
from core.provenance.codec import MAX_PAYLOAD_BYTES


class TransportDisconnected(ConnectionError):
    """Transport-only failure, distinct from an application sink exception."""


class LiveMQTT:
    def __init__(self, settings: TLSSettings, pipeline: ProvenancePipeline, *, capacity: int = 128):
        if type(capacity) is not int or not 1 <= capacity <= 10000:
            raise ValueError('invalid receive queue capacity')
        self.pipeline = pipeline
        self.connection = MQTTConnection(settings)
        self.connection.client.manual_ack_set(True)
        self.incoming = queue.Queue(maxsize=capacity)
        self.overflow = threading.Event()
        self.connection.client.on_message = self._on_message

    def _on_message(self, client, userdata, message):
        if len(message.payload) > MAX_PAYLOAD_BYTES:
            self.overflow.set()
            return
        try:
            self.incoming.put_nowait((message.topic, bytes(message.payload), message.mid, message.qos))
        except queue.Full:
            self.overflow.set()

    def __enter__(self):
        self.connection.connect()
        try:
            self.connection.subscribe('sensors/raw/+')
        except BaseException:
            self.connection.close()
            raise
        return self

    def receive(self, timeout: float = 10.0):
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('receive timeout must be finite and in (0,3600]')
        deadline = time.monotonic() + timeout
        while True:
            if self.overflow.is_set():
                raise BufferError('receiver queue overflow; stop and recover missing records')
            if self.connection.failure or not self.connection.connected.is_set():
                raise TransportDisconnected(self.connection.failure or 'receiver disconnected')
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('receiver timed out waiting for a message')
            try:
                topic, payload, mid, qos = self.incoming.get(timeout=min(remaining,0.25))
                break
            except queue.Empty:
                continue
        verdict = self.pipeline.receive(payload, topic=topic)
        if qos > 0 and self.connection.client.ack(mid, qos) != mqtt.MQTT_ERR_SUCCESS:
            raise TransportDisconnected('receiver PUBACK failed; verdict remains durable')
        return verdict

    def __exit__(self, *args):
        self.connection.close()
