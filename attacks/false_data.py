"""Bounded, deterministic pre-signing transformations for controlled experiments."""
from dataclasses import dataclass
import hashlib
import math
import random
from typing import Literal

from pydantic import Field, model_validator
from adapters.dataset import Measurement
from core.contracts import Contract, FiniteNumber, Identifier
from core.processing.engine import bounded

Family = Literal['bias', 'slow_drift', 'spike', 'noise_flatten', 'freeze',
                 'synthetic_mimicry', 'pattern_masking', 'colluding_neighbors']


class AttackConfig(Contract):
    family: Family
    sensors: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    seed: int = Field(default=0, ge=0, le=2**63-1)
    start_index: int = Field(default=0, ge=0, le=9999)
    stop_index: int = Field(default=10000, ge=1, le=10000)
    offset_c: FiniteNumber = 5.0
    drift_c_per_second: FiniteNumber = 0.1
    pulse_every: int = Field(default=8, ge=1, le=10000)
    pulse_width: int = Field(default=1, ge=1, le=10000)
    noise_std_c: FiniteNumber = Field(default=0.5, ge=0.0)
    flatten_factor: FiniteNumber = Field(default=0.0, ge=0.0, le=1.0)
    mask_weight: FiniteNumber = Field(default=0.1, ge=0.0, le=1.0)
    template_c: tuple[FiniteNumber, ...] = Field(default=(), max_length=10000)

    @model_validator(mode='after')
    def coherent(self):
        if len(set(self.sensors)) != len(self.sensors):
            raise ValueError('duplicate attack sensor')
        if self.stop_index <= self.start_index or self.pulse_width > self.pulse_every:
            raise ValueError('invalid attack interval or pulse width')
        if self.family == 'colluding_neighbors' and len(self.sensors) < 2:
            raise ValueError('collusion requires at least two sensors')
        if self.family in ('synthetic_mimicry', 'pattern_masking') and not self.template_c:
            raise ValueError('an explicit benign template is required')
        return self


@dataclass(frozen=True)
class AttackLabel:
    """Evaluation-only sidecar; never passed to the detector or FeatureWindow."""
    source_id: str
    source_record_id: str
    sensor_id: str
    sample_index: int
    family: str
    applied: bool
    changed: bool
    original_value: float | None
    injected_value: float | None


@dataclass(frozen=True)
class AttackBatch:
    measurements: tuple[Measurement, ...]
    labels: tuple[AttackLabel, ...]
    config: AttackConfig


def inject(measurements, config: AttackConfig) -> AttackBatch:
    config = AttackConfig.model_validate(config)
    samples = tuple(Measurement.model_validate(m) for m in bounded(measurements, 10000, 'attack batch'))
    counts, previous, anchors, randoms = {}, {}, {}, {}
    output, labels = [], []
    for sample in samples:
        sensor = sample.sensor_id
        index = counts.get(sensor, 0)
        counts[sensor] = index + 1
        if sensor in previous and sample.observed_at < previous[sensor]:
            raise ValueError('input must be chronological per sensor')
        previous[sensor] = sample.observed_at
        active = sensor in config.sensors and config.start_index <= index < config.stop_index
        value = sample.value
        applied = active and value is not None
        if active and (sample.unit != 'C' or sample.quantity != 'temperature'):
            raise ValueError('attack input must explicitly be Celsius temperature')
        if applied:
            anchor_value, anchor_time = anchors.setdefault(sensor, (value, sample.observed_at))
            relative = index - config.start_index
            if config.family in ('bias', 'colluding_neighbors'):
                value += config.offset_c
            elif config.family == 'slow_drift':
                value += config.drift_c_per_second * (sample.observed_at-anchor_time).total_seconds()
            elif config.family == 'spike':
                applied = relative % config.pulse_every < config.pulse_width
                if applied:
                    value += config.offset_c
            elif config.family == 'freeze':
                value = anchor_value
            elif config.family == 'noise_flatten':
                seed = hashlib.sha256(f'{config.seed}:{sensor}'.encode()).digest()
                rng = randoms.setdefault(sensor, random.Random(seed))
                value = anchor_value + config.flatten_factor*(value-anchor_value) + rng.gauss(0.0, config.noise_std_c)
            elif config.family == 'synthetic_mimicry':
                value = config.template_c[relative % len(config.template_c)]
            elif config.family == 'pattern_masking':
                template = config.template_c[relative % len(config.template_c)]
                value = config.mask_weight*value + (1.0-config.mask_weight)*template
            if not math.isfinite(value):
                raise ValueError('attack arithmetic overflow')
        changed = value != sample.value
        output.append(Measurement(**{**sample.model_dump(), 'value': value}))
        labels.append(AttackLabel(sample.source_id, sample.source_record_id, sensor, index,
                                 config.family, applied, changed, sample.value, value))
    missing = set(config.sensors) - set(counts)
    if missing:
        raise ValueError('attack targets absent from input')
    return AttackBatch(tuple(output), tuple(labels), config)
