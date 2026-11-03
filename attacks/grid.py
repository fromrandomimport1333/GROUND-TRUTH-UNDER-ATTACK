"""Grid configs: expand a parameter grid into *unplaced* scenario specs.

A grid YAML lists, per fault family, the swept parameter values (in sigma_ref units, or
hours for anything that is a duration) plus the window durations. ``expand`` takes the
Cartesian product (parameters x durations x applicable variables x replicates) and emits
specs of the form ``{family, variable, params, duration_steps}``. Choosing stations and
start times ("placement") is a later step (``attacks.placement``).

YAML conventions for ``families``
---------------------------------
* A family maps each *size* parameter (``k``, ``n``, ``g``, ``f``, ``total_mm``) to a list.
  The keys are exactly ``schema.REQUIRED_PARAMS[family]`` minus the window-mirroring key.
* Durations are in **hours** and become 5-minute steps (x12) in ``duration_steps``:
    - ``duration_h`` (optional) overrides the config-wide ``durations_h`` for that family
      (e.g. ``spike`` and ``multi_offset`` use one fixed window, ``rain_scale`` its own list).
    - ``drift`` takes ``ramp_h``: ramp length *is* the window (no separate duration list).
    - ``flatline`` takes ``w_h``: the rolling-mean width *is* the window; it is stored as
      ``params["w_steps"]`` and equals ``duration_steps``.
    - ``rain_inject`` takes a required ``duration_h``: the event duration *is* the window;
      it is stored both as ``duration_steps`` and as ``params["duration_steps"]``.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from attacks.schema import REQUIRED_PARAMS, Family, Variable

STEPS_PER_HOUR = 12  # 5-minute data

VALUE_VARIABLES: tuple[Variable, ...] = ("air_temperature", "relative_humidity")
PRECIP_VARIABLE: Variable = "precipitation"

# Shapes that only the held-out suite may contain (spec section 5/6).
HELDOUT_ONLY: frozenset[str] = frozenset({"gain", "intermittent_offset"})

# Families whose window is pinned by a size/ramp parameter rather than a duration list:
# family -> YAML key holding the (hour) list that defines the window.
_SELF_WINDOW_KEY: dict[str, str] = {
    "drift": "ramp_h",
    "flatline": "w_h",
    "rain_inject": "duration_h",
}
# Of those, the ones whose window length is also a schema param (in steps).
_WINDOW_PARAM: dict[str, str] = {
    "flatline": "w_steps",
    "rain_inject": "duration_steps",
}
_DURATION_KEY = "duration_h"  # optional per-family override for every other family


def _size_keys(family: str) -> list[str]:
    """YAML keys for the family's swept size parameters (sorted, deterministic)."""
    return sorted(REQUIRED_PARAMS[family] - {_WINDOW_PARAM.get(family, "")})


def _steps(hours: float, *, where: str) -> int:
    raw = hours * STEPS_PER_HOUR
    steps = round(raw)
    if abs(raw - steps) > 1e-9 or steps < 1:
        raise ValueError(
            f"{where}: {hours} h is not a positive whole number of 5-minute steps"
        )
    return steps


def _applicable_variables(cfg_variables: list[str], family: str) -> list[str]:
    if family.startswith("rain_"):
        return [v for v in cfg_variables if v == PRECIP_VARIABLE]
    return [v for v in cfg_variables if v in VALUE_VARIABLES]


class GridConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    seed: int
    split: Literal["smoke", "dev", "heldout"]
    variables: list[Variable]
    months: list[int] | None = None
    station_pool: Literal["dev", "heldout", "all"]
    max_stations: int | None = None
    replicates: int
    # Replicates for precipitation families when they differ (dev uses 3 vs 2).
    precip_replicates: int | None = None
    families: dict[Family, dict[str, list[float]]]
    durations_h: list[float]

    @field_validator("months")
    @classmethod
    def _months_valid(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and (not v or any(not 1 <= m <= 12 for m in v)):
            raise ValueError(f"months must be a non-empty list of 1..12, got {v}")
        return v

    @field_validator("replicates", "precip_replicates", "max_stations")
    @classmethod
    def _positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("must be >= 1")
        return v

    @model_validator(mode="after")
    def _check(self) -> GridConfig:
        for hours in self.durations_h:
            _steps(hours, where=f"{self.name}.durations_h")

        for family, fam in self.families.items():
            where = f"{self.name}.families.{family}"

            if family in HELDOUT_ONLY and self.split != "heldout":
                raise ValueError(
                    f"{where}: {family!r} is heldout-only but split is {self.split!r}"
                )
            if not _applicable_variables(list(self.variables), family):
                raise ValueError(
                    f"{where}: no applicable variable in variables={self.variables}"
                )

            size_keys = set(_size_keys(family))
            window_key = _SELF_WINDOW_KEY.get(family, _DURATION_KEY)
            window_required = family in _SELF_WINDOW_KEY
            allowed = size_keys | {window_key}
            required = size_keys | ({window_key} if window_required else set())

            given = set(fam)
            if given - allowed:
                raise ValueError(
                    f"{where}: unexpected keys {sorted(given - allowed)}; "
                    f"allowed {sorted(allowed)}"
                )
            if required - given:
                raise ValueError(f"{where}: missing keys {sorted(required - given)}")
            for key, values in fam.items():
                if not values:
                    raise ValueError(f"{where}.{key}: empty list")
                if key == window_key:
                    for h in values:
                        _steps(h, where=f"{where}.{key}")
            if not window_required and window_key not in fam and not self.durations_h:
                raise ValueError(
                    f"{where}: no {_DURATION_KEY} and durations_h is empty"
                )
        return self


def load_grid(path: Path) -> GridConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GridConfig.model_validate(data)


def expand(cfg: GridConfig) -> list[dict]:
    """Cartesian product of params x durations x variables x replicates (unplaced specs).

    Order is deterministic: families in YAML order, then variables in config order, then
    size parameters (sorted by key), then durations, then replicates. Replicates are
    independent copies of the same spec.
    """
    specs: list[dict] = []
    for family, fam in cfg.families.items():
        size_keys = _size_keys(family)
        window_key = _SELF_WINDOW_KEY.get(family, _DURATION_KEY)
        windows_h = fam.get(window_key, cfg.durations_h)
        window_param = _WINDOW_PARAM.get(family)

        for variable in _applicable_variables(list(cfg.variables), family):
            if variable == PRECIP_VARIABLE and cfg.precip_replicates is not None:
                reps = cfg.precip_replicates
            else:
                reps = cfg.replicates

            for combo in itertools.product(*(fam[key] for key in size_keys)):
                for hours in windows_h:
                    steps = _steps(hours, where=f"{cfg.name}.{family}")
                    params = {key: float(v) for key, v in zip(size_keys, combo)}
                    if window_param is not None:
                        params[window_param] = float(steps)
                    for _ in range(reps):
                        specs.append(
                            {
                                "family": family,
                                "variable": variable,
                                "params": dict(params),
                                "duration_steps": steps,
                            }
                        )
    return specs
