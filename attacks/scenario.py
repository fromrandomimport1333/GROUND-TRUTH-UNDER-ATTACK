"""Test-only signing of prepared measurements; no production credentials accepted."""
from dataclasses import dataclass
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from adapters.dataset import Measurement
from adapters.replay import ReplayRecord
from core.processing.engine import bounded
from core.provenance.codec import encode_reading
from core.provenance.keys import key_id
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState


@dataclass(frozen=True)
class Recording:
    records: tuple[ReplayRecord, ...]
    enrollment: tuple[tuple[str, bytes, str], ...]
    source_records: tuple[tuple[str, str], ...]

    def enroll(self, registry):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        for sensor, public, stream in self.enrollment:
            registry.enroll(sensor, Ed25519PublicKey.from_public_bytes(public), stream)


def sign_experiment(measurements, directory: Path) -> Recording:
    """Generate fresh ephemeral keys; stable replay uses the returned recording.

    Signing here authenticates this experiment's generator, not the original
    dataset's source. No private keys are exported. Output order follows input.
    Caller owns directory lifetime; it must be a new path.
    """
    samples = tuple(Measurement.model_validate(m) for m in bounded(measurements, 10000, 'signing batch'))
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    signers, enrollment, records, sources = {}, [], [], []
    for sample in samples:
        sensor = sample.sensor_id
        if sensor not in signers:
            key = Ed25519PrivateKey.generate()
            stream = 'experiment_01'
            # Index filenames avoid using externally supplied sensor IDs as paths.
            state = SignerState.create(directory / f'signer_{len(signers)}.sqlite3',
                                       [(sensor, key_id(key), stream)])
            signers[sensor] = SensorSigner(sensor, stream, key, state)
            enrollment.append((sensor, key.public_key().public_bytes_raw(), stream))
        signer = signers[sensor]
        reading = signer.sign(sample.value, unit=sample.unit, quantity=sample.quantity,
                              timestamp=sample.observed_at, location=sample.location)
        payload = encode_reading(reading)
        records.append(ReplayRecord(payload=payload, received_at=sample.observed_at,
                                    topic=f'sensors/raw/{sensor}'))
        sources.append((sample.source_id, sample.source_record_id))
        # Recording holds the exact bytes; discard the local delivery outbox entry.
        signer.state.acknowledge(reading)
    return Recording(tuple(records), tuple(enrollment), tuple(sources))


class DatasetExperiment:
    """Offline-only persistent experiment using production signer and verifier.

    Publicly reproducible test keys, NEVER source authentication or production
    credentials. Records and public enrollment are exported for independent replay.
    Durable local states are temporary; audit evidence is retained by the caller.
    """
    def __init__(self, directory, sensors, seed, *, offline_memory=False):
        import hashlib
        from core.provenance.keyregistry import KeyRegistry
        from core.provenance.verifier import Verifier
        directory = Path(directory)
        self.registry = KeyRegistry.create(directory/'receiver.sqlite3')
        self.signers = {}
        keys = {}
        identities = []
        for sensor in sorted(sensors):
            key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(
                ('CIS PUBLIC OFFLINE TEST KEY v1|'+seed+'|'+sensor).encode()).digest())
            keys[sensor] = key
            identities.append((sensor,key_id(key),'dataset_experiment_v1'))
            self.registry.enroll(sensor,key.public_key(),'dataset_experiment_v1')
        state = SignerState.create(directory/'signer.sqlite3',identities)
        self.offline_memory = offline_memory
        if offline_memory:
            from attacks.offline_state import OfflineSignerState, OfflineKeyRegistry
            state = OfflineSignerState(state)
            self.registry = OfflineKeyRegistry(self.registry)
        self.state = state
        for sensor,key in keys.items():
            self.signers[sensor] = SensorSigner(sensor,'dataset_experiment_v1',key,state)
        self.enrollment = [{'sensor_id':s,'public_key_hex':k.public_key().public_bytes_raw().hex(),
                            'key_id':key_id(k),'stream_id':'dataset_experiment_v1'} for s,k in keys.items()]
        self.now = None
        self.verifier = Verifier(self.registry,clock=lambda:self.now)

    def verify(self, sample):
        signer = self.signers[sample.sensor_id]
        reading = signer.sign(sample.value,unit=sample.unit,quantity=sample.quantity,
                              timestamp=sample.observed_at,location=sample.location)
        self.now = sample.observed_at
        payload = encode_reading(reading)
        event_id,event = self.verifier.verify(payload,topic=f'sensors/raw/{sample.sensor_id}')
        signer.state.acknowledge(reading)
        return event_id,event,payload

    def discard_delivered(self):
        if self.offline_memory:
            self.registry.discard_delivered()

    def close(self):
        if self.offline_memory:
            self.state.close()
            self.registry.close()
