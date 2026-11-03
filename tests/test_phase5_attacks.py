from datetime import timedelta
from typing import get_args
import pytest
from pydantic import ValidationError

from adapters.dataset import Measurement
from attacks.false_data import AttackConfig, Family, inject
from scripts.demo_attacks import synthetic_measurements


def samples():
    return tuple(m for m in synthetic_measurements() if m.sensor_id == 'sensor_01')


def config(family, **kwargs):
    return AttackConfig(family=family, sensors=('sensor_01',), **kwargs)


@pytest.mark.parametrize('family', get_args(Family))
def test_all_families_deterministic_bounded_preserve_evidence(family):
    original = synthetic_measurements()
    cfg = AttackConfig(family=family, sensors=('sensor_01','sensor_02'), start_index=3, stop_index=12,
                       seed=12, template_c=(21.0,22.0))
    a, b = inject(original,cfg), inject(original,cfg)
    assert a == b
    assert any(x.changed for x in a.labels)
    for before, after, label in zip(original,a.measurements,a.labels):
        assert before.model_dump(exclude={'value'}) == after.model_dump(exclude={'value'})
        assert label.original_value == before.value and label.injected_value == after.value
        if before.sensor_id == 'sensor_03' or not 3 <= label.sample_index < 12:
            assert before == after and not label.applied


def test_exact_bias_drift_pulse_freeze_and_flatten():
    source = samples()
    assert inject(source,config('bias',offset_c=2.0)).measurements[3].value == source[3].value+2
    drift = inject(source,config('slow_drift',start_index=2,drift_c_per_second=0.5))
    assert drift.measurements[5].value == source[5].value+3.0
    pulse = inject(source,config('spike',pulse_every=4,pulse_width=2))
    assert [l.applied for l in pulse.labels[:8]] == [True,True,False,False]*2
    frozen = inject(source,config('freeze',start_index=2,stop_index=5))
    assert [m.value for m in frozen.measurements[2:5]] == [source[2].value]*3
    flat = inject(source,config('noise_flatten',noise_std_c=0.0,flatten_factor=0.0))
    assert len({m.value for m in flat.measurements}) == 1


def test_explicit_template_mask_and_collusion():
    source = samples()
    mimic = inject(source,config('synthetic_mimicry',template_c=(1.0,2.0)))
    assert [m.value for m in mimic.measurements[:4]] == [1.0,2.0,1.0,2.0]
    masked = inject(source,config('pattern_masking',template_c=(10.0,),mask_weight=0.25))
    assert masked.measurements[0].value == source[0].value*.25+7.5
    batch = inject(synthetic_measurements(),AttackConfig(family='colluding_neighbors',sensors=('sensor_01','sensor_02'),offset_c=3.0))
    assert all(abs(l.injected_value-l.original_value-3.0)<1e-12 for l in batch.labels if l.sensor_id!='sensor_03')


def test_noise_seed_changes_output_and_interleaving_does_not():
    a = inject(samples(),config('noise_flatten',seed=1))
    b = inject(samples(),config('noise_flatten',seed=2))
    assert a.measurements != b.measurements
    all_sensors = inject(synthetic_measurements(),config('noise_flatten',seed=1))
    assert a.measurements == tuple(m for m in all_sensors.measurements if m.sensor_id=='sensor_01')


@pytest.mark.parametrize('changes', [{'seed':True},{'start_index':-1},{'stop_index':0},
    {'pulse_width':9},{'noise_std_c':float('nan')},{'sensors':('sensor_01','sensor_01')},
    {'family':'synthetic_mimicry'},{'family':'colluding_neighbors'}, {'mask_weight':2.0}])
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        AttackConfig(**{'family':'bias','sensors':('sensor_01',),**changes})


def test_missing_non_celsius_order_and_overflow():
    source = samples()
    null = Measurement(**{**source[0].model_dump(),'value':None})
    assert inject((null,*source[1:]),config('freeze')).measurements[0].value is None
    with pytest.raises(ValueError): inject((source[1],source[0]),config('bias'))
    with pytest.raises(ValueError): inject(source,AttackConfig(family='bias',sensors=('absent',)))
    with pytest.raises(ValueError): inject((Measurement(**{**source[0].model_dump(),'unit':'F'}),),config('bias'))
    huge = Measurement(**{**source[0].model_dump(),'value':1e308})
    with pytest.raises(ValueError): inject((huge,),config('bias',offset_c=1e308))
    with pytest.raises(BufferError): inject((source[0] for _ in range(10001)),config('bias'))
