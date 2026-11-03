"""Eight reproducible synthetic false-data experiments; no detector metrics."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from typing import get_args

from adapters.dataset import Measurement
from adapters.replay import ReplayAdapter
from attacks.false_data import AttackConfig, Family, inject
from attacks.scenario import sign_experiment
from attacks.security_demo import run_security_demo
from core.contracts import FeatureWindow, Location
from core.processing.policy import ProcessingPolicy
from core.provenance.keyregistry import KeyRegistry


def synthetic_measurements():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(Measurement(sensor_id=f'sensor_{s:02d}', observed_at=start+timedelta(seconds=i*2),
                 value=20.0+i*0.25+s*0.1, source_id='synthetic_demo', source_record_id=f'{s}:{i}',
                 location=Location(latitude=36.8+s*0.001, longitude=10.1))
                 for i in range(16) for s in range(1, 4))


def run_demo(root):
    samples = synthetic_measurements()
    as_of = samples[0].observed_at + timedelta(seconds=60)
    summaries = []
    for family in get_args(Family):
        config = AttackConfig(family=family, sensors=('sensor_01', 'sensor_02') if family == 'colluding_neighbors' else ('sensor_01',),
                              start_index=4, stop_index=14, seed=42, template_c=(20.2, 20.3, 20.1))
        batch = inject(samples, config)
        recording = sign_experiment(batch.measurements, root / family)
        registry = KeyRegistry.create(root / f'{family}.sqlite3')
        recording.enroll(registry)
        received, audit = [], []
        def layer3_stub(window):
            received.append(FeatureWindow.model_validate(window))
        result = ReplayAdapter(registry, layer4=lambda i,v: audit.append(v), layer3=layer3_stub,
                     processing_policy=ProcessingPolicy(neighbor_pairs=(('sensor_01','sensor_02'),('sensor_01','sensor_03')))).run(recording.records, as_of=as_of)
        if len(received) != 1 or len(audit) != 48 or any(v.status != 'valid' for v in result.events):
            raise RuntimeError('signed attack integration failed')
        summaries.append({'family': family, 'statuses': dict(Counter(v.status for v in result.events)),
                          'changed_measurements': sum(label.changed for label in batch.labels),
                          'normalized_readings': sum(p.ready_for_analysis for p in received[0].readings),
                          'layer3_stub_received': len(received), 'detection_evaluated': False})
    return {'input_source': 'synthetic_demo', 'experiments': summaries,
            'security_scenarios': run_security_demo(root),
            'note': 'Fresh keys per experiment; replay of captured bytes is deterministic. Layer 3 is a contract receiver only.'}


def main():
    with tempfile.TemporaryDirectory(prefix='cis-phase5-') as directory:
        print(json.dumps(run_demo(Path(directory)), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
