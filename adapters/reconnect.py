"""Bounded transport retries. Authentication failures and sink failures propagate."""
from dataclasses import dataclass
import math
import ssl
import time

from adapters.mqtt_transport import MQTTConnection
from adapters.live_mqtt import LiveMQTT, TransportDisconnected


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 5
    initial_delay: float = 0.25
    maximum_delay: float = 2.0

    def __post_init__(self):
        if type(self.attempts) is not int or not 1 <= self.attempts <= 20:
            raise ValueError('attempts must be between 1 and 20')
        if any(type(x) not in (int,float) or not math.isfinite(x) or not 0 <= x <= 30 for x in (self.initial_delay,self.maximum_delay)):
            raise ValueError('retry delays must be finite and bounded')
        if self.initial_delay > self.maximum_delay:
            raise ValueError('initial delay exceeds maximum')

    def delay(self, failure_index):
        return min(self.maximum_delay, self.initial_delay * 2**failure_index)


def retryable(error):
    return isinstance(error, (ConnectionError,TimeoutError,OSError)) and not isinstance(error,(PermissionError,ssl.SSLError))


class ReconnectingPublisher:
    """Retry identical bytes after uncertain PUBACK; verifier deduplicates repeats."""
    def __init__(self, settings, *, policy=None, factory=MQTTConnection, sleep=time.sleep):
        self.settings, self.policy = settings, policy or RetryPolicy()
        self.factory, self.sleep = factory, sleep
        self.connection = None
        self.connection_attempts = 0

    def connect(self):
        for attempt in range(self.policy.attempts):
            try:
                if self.connection is None:
                    self.connection_attempts += 1
                    self.connection = self.factory(self.settings)
                    self.connection.connect()
                return
            except Exception as error:
                self.close()
                if not retryable(error) or attempt+1 == self.policy.attempts:
                    raise
                self.sleep(self.policy.delay(attempt))

    def publish(self, sensor_id, payload):
        for attempt in range(self.policy.attempts):
            try:
                if self.connection is None:
                    self.connection_attempts += 1
                    connection = self.factory(self.settings)
                    self.connection = connection
                    connection.connect()
                self.connection.publish(sensor_id, payload)
                return
            except Exception as error:
                self.close()
                if not retryable(error) or attempt+1 == self.policy.attempts:
                    raise
                self.sleep(self.policy.delay(attempt))

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()


class ReconnectingReceiver:
    """New clean sessions after disconnect; archives are needed for missed data.

    Idle timeouts, queue overflow and application callback failures propagate.
    Reconnection never resets the verifier database or skips a missing sequence.
    """
    def __init__(self, settings, pipeline, *, policy=None, factory=LiveMQTT, sleep=time.sleep):
        self.settings, self.pipeline = settings, pipeline
        self.policy, self.factory, self.sleep = policy or RetryPolicy(), factory, sleep
        self.receiver = None
        self.connection_attempts = 0

    def _connect(self):
        self.connection_attempts += 1
        receiver = self.factory(self.settings,self.pipeline)
        self.receiver = receiver
        receiver.__enter__()

    def receive(self, timeout=10.0):
        for attempt in range(self.policy.attempts):
            if self.receiver is None:
                while self.pipeline.drain():
                    pass
                try:
                    self._connect()
                except Exception as error:
                    self.close()
                    if not retryable(error) or attempt+1 == self.policy.attempts:
                        raise
                    self.sleep(self.policy.delay(attempt))
                    continue
            try:
                return self.receiver.receive(timeout)
            except TransportDisconnected:
                self.close()
                if attempt+1 == self.policy.attempts:
                    raise
                self.sleep(self.policy.delay(attempt))

    def __enter__(self):
        # Drain callbacks outside transport retry handling.
        while self.pipeline.drain():
            pass
        # Establish subscription before reporting receiver readiness.
        for attempt in range(self.policy.attempts):
            try:
                self._connect()
                return self
            except Exception as error:
                self.close()
                if not retryable(error) or attempt+1 == self.policy.attempts:
                    raise
                self.sleep(self.policy.delay(attempt))

    def close(self):
        receiver, self.receiver = self.receiver, None
        if receiver is not None:
            receiver.__exit__(None,None,None)

    def __exit__(self,*args):
        self.close()
