"""Explicit, immutable prototype policies; not official challenge parameters."""
from typing import Annotated
from pydantic import Field, StrictFloat, StrictInt, model_validator
from core.contracts import Contract, Identifier

Seconds = Annotated[StrictFloat, Field(ge=0, le=86400)]


class ProcessingPolicy(Contract):
    window_seconds: Annotated[StrictInt, Field(ge=1, le=86400)] = 60
    rolling_samples: Annotated[StrictInt, Field(ge=2, le=10000)] = 5
    constant_tolerance_c: Annotated[StrictFloat, Field(ge=0, le=1000)] = 0.0
    spectral_min_samples: Annotated[StrictInt, Field(ge=8, le=10000)] = 8
    spectral_relative_jitter: Annotated[StrictFloat, Field(ge=0, le=0.1)] = 0.01
    max_events: Annotated[StrictInt, Field(ge=1, le=20000)] = 10000
    max_sensors: Annotated[StrictInt, Field(ge=1, le=100)] = 100
    max_references: Annotated[StrictInt, Field(ge=1, le=10000)] = 10000
    neighbor_pairs: tuple[tuple[Identifier, Identifier], ...] = Field(default=(), max_length=10000)
    spatial_max_age_seconds: Seconds = 10.0
    spatial_max_distance_km: Annotated[StrictFloat, Field(gt=0, le=20040)] = 50.0
    reference_sources: tuple[Identifier, ...] = Field(default=(), max_length=100)
    reference_max_age_seconds: Seconds = 3600.0
    reference_max_distance_km: Annotated[StrictFloat, Field(gt=0, le=20040)] = 50.0

    @model_validator(mode='after')
    def unique_configuration(self):
        if len(set(self.neighbor_pairs)) != len(self.neighbor_pairs):
            raise ValueError('duplicate neighbor pairs')
        if any(a == b for a, b in self.neighbor_pairs):
            raise ValueError('a sensor cannot be its own neighbor')
        if len(set(self.reference_sources)) != len(self.reference_sources):
            raise ValueError('duplicate reference sources')
        return self
