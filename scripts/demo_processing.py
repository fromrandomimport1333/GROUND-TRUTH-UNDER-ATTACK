"""Deterministic synthetic Phase 4 demonstration with real Ed25519 verification."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from core.contracts import Location, ReferenceObservation
from core.processing.engine import ProcessingEngine
from core.processing.policy import ProcessingPolicy
from core.provenance.codec import encode_reading
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import key_id
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier


def main():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=60)
    events = []
    with tempfile.TemporaryDirectory(prefix='cis-processing-') as directory:
        root = Path(directory)
        registry = KeyRegistry.create(root / 'receiver.sqlite3')
        verifier = Verifier(registry, clock=lambda: end)
        for index, unit in enumerate(('C', 'F', 'K'), 1):
            sensor = f'sensor_{index:02d}'
            key = Ed25519PrivateKey.generate()
            registry.enroll(sensor, key.public_key(), 'stream_01')
            state = SignerState.create(root / f'{sensor}.sqlite3', [(sensor, key_id(key), 'stream_01')])
            signer = SensorSigner(sensor, 'stream_01', key, state)
            for step in range(16):
                value_c = 20.0 + step * 0.25 + index * 0.1
                value = value_c if unit == 'C' else value_c * 1.8 + 32 if unit == 'F' else value_c + 273.15
                raw = signer.sign(value, unit=unit, timestamp=start+timedelta(seconds=step*2),
                                  location=Location(latitude=36.8+index*0.001, longitude=10.1))
                events.append(verifier.verify(encode_reading(raw))[1])
        # Explicitly synthetic reference, never represented as official weather data.
        reference = ReferenceObservation(source_id='synthetic_demo', observed_at=start,
            available_at=start, quantity='temperature', value=20.0, unit='C',
            location=Location(latitude=36.8, longitude=10.1))
        policy = ProcessingPolicy(neighbor_pairs=(('sensor_01','sensor_02'), ('sensor_01','sensor_03')),
                                  reference_sources=('synthetic_demo',))
        result = ProcessingEngine(policy).build_window(events, start=start, end=end, as_of=end,
                                                       references=(reference,))
        assert len(result.window.readings) == 48
        assert all(p.ready_for_analysis for p in result.window.readings)
        print('PHASE 4 DEMO PASS — synthetic inputs; real signatures; 48 normalized readings')
        print(result.window.model_dump_json(indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
