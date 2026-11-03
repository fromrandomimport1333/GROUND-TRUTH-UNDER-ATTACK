"""Scenario / suite schema: the contract every later attack module builds on."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

Family = Literal[
    "offset",
    "drift",
    "spike",
    "noise",
    "flatline",
    "stuck",
    "multi_offset",
    "gain",
    "intermittent_offset",
    "rain_scale",
    "rain_suppress",
    "rain_inject",
    "realistic_replace",
    "cycle_shaped",
    "network_bias",
    "value_replay",
]

Variable = Literal["air_temperature", "relative_humidity", "precipitation"]
Split = Literal["smoke", "dev", "heldout", "showcase"]

REQUIRED_PARAMS: dict[Family, set[str]] = {
    "offset": {"k"},
    "drift": {"k"},
    "spike": {"k", "n"},
    "noise": {"k"},
    "flatline": {"w_steps"},
    "stuck": set(),
    "multi_offset": {"k", "n"},
    "gain": {"g"},
    "intermittent_offset": {"k"},
    "rain_scale": {"f"},
    "rain_suppress": set(),
    "rain_inject": {"total_mm", "duration_steps"},
    # generator is a float code: 0.0 = empirical resample, 1.0 = AR(1) (params is float-only)
    "realistic_replace": {"k", "generator"},
    "cycle_shaped": {"k"},
    "network_bias": {"k"},
    "value_replay": {"days_back", "noise"},
}

RAIN_FAMILIES: frozenset[str] = frozenset(
    f for f in get_args(Family) if f.startswith("rain_")
)

_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True)


class Scenario(BaseModel):
    model_config = _MODEL_CONFIG

    id: str
    family: Family
    variable: Variable
    stations: tuple[str, ...]
    start: AwareDatetime
    end: AwareDatetime
    params: dict[str, float]
    split: Split

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        if self.end <= self.start:
            raise ValueError(
                f"{self.id}: end ({self.end}) must be after start ({self.start})"
            )

        is_rain = self.family in RAIN_FAMILIES
        if is_rain and self.variable != "precipitation":
            raise ValueError(
                f"{self.id}: family {self.family!r} requires variable 'precipitation', "
                f"got {self.variable!r}"
            )
        if not is_rain and self.variable == "precipitation":
            raise ValueError(
                f"{self.id}: family {self.family!r} is not a precipitation family; "
                "use a rain_* family for precipitation"
            )

        required = REQUIRED_PARAMS[self.family]
        given = set(self.params)
        if given != required:
            missing = sorted(required - given)
            extra = sorted(given - required)
            raise ValueError(
                f"{self.id}: params for {self.family!r} must be exactly "
                f"{sorted(required)}; missing={missing}, unexpected={extra}"
            )
        return self


class Suite(BaseModel):
    model_config = _MODEL_CONFIG

    name: str
    seed: int
    scenarios: tuple[Scenario, ...]


def _iso_z(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.replace(tzinfo=None).isoformat(timespec=timespec) + "Z"


def _to_plain(suite: Suite) -> dict:
    """Plain-python (YAML-safe) view of a suite: lists, str times, float params."""
    return {
        "name": suite.name,
        "seed": suite.seed,
        "scenarios": [
            {
                "id": s.id,
                "family": s.family,
                "variable": s.variable,
                "stations": list(s.stations),
                "start": _iso_z(s.start),
                "end": _iso_z(s.end),
                "params": {key: float(val) for key, val in s.params.items()},
                "split": s.split,
            }
            for s in suite.scenarios
        ],
    }


def load_suite(path: Path) -> Suite:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return Suite.model_validate(data)


def dump_suite(suite: Suite, path: Path) -> None:
    text = yaml.safe_dump(
        _to_plain(suite),
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
