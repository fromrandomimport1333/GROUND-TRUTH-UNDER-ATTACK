"""Escape-magnitude evaluation: measure a detector's blind-spot boundary per fault family.

This is defensive robustness evaluation. For a base scenario it sweeps the one size parameter
over a grid and reports the LARGEST magnitude the detector still fails to catch -- the honest
"below this, residual risk" number the report quotes. It uses the detector's own responses, so
it is an adaptive evaluation; it only ever probes the team's own detector.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from attacks.injector import inject
from attacks.schema import Scenario, Suite
from eval.scoring import event_table

# the single swept size parameter per family (families without one cannot "escape" by size)
SIZE_KEY = {
    "offset": "k", "drift": "k", "spike": "k", "noise": "k", "gain": "g",
    "multi_offset": "k", "intermittent_offset": "k", "network_bias": "k",
    "realistic_replace": "k", "cycle_shaped": "k", "rain_scale": "f", "rain_inject": "total_mm",
}


def _variant(scenario: Scenario, size: float) -> Scenario:
    key = SIZE_KEY[scenario.family]
    params = dict(scenario.params)
    params[key] = float(size)
    return scenario.model_copy(update={"params": params})


def _is_detected(scenario: Scenario, clean, sigma, detect, grace: str) -> bool:
    suite = Suite(name="probe", seed=0, scenarios=(scenario,))
    faulted, labels = inject(suite, clean, sigma)
    alarms = detect(faulted)
    if labels.empty:
        return False
    events = event_table(labels, alarms, grace=grace)
    return bool(events["detected"].any())


def escape_magnitude(scenario, clean, sigma, detect, *, grid, grace: str = "1h") -> float:
    """Largest size in ``grid`` the detector fails to catch for ``scenario`` (NaN if none).

    Sizes are tried in ascending order; the sweep stops at the first detected size (larger
    faults are assumed at least as detectable). Returns NaN if even the smallest grid size
    is caught, or if the family has no size parameter to sweep.
    """
    if scenario.family not in SIZE_KEY:
        return math.nan
    escaped = math.nan
    for size in sorted(grid):
        if _is_detected(_variant(scenario, size), clean, sigma, detect, grace):
            break
        escaped = float(size)
    return escaped


def build_adaptive_suite(base_scenarios, clean, sigma, detect, *, grid, grace: str = "1h",
                         pool: str = "heldout") -> Suite:
    """One scenario per base family, each set to the detector's escape magnitude for it.

    A base whose escape magnitude is NaN (always caught, or no size parameter) is placed at the
    smallest grid size so the suite still records the attempt; ``escape_size`` on the scenario id
    records the boundary.
    """
    scenarios = []
    for base in base_scenarios:
        if base.family not in SIZE_KEY:  # no size to sweep (flatline, stuck, rain_suppress)
            scenarios.append(base.model_copy(update={"split": pool}))
            continue
        esc = escape_magnitude(base, clean, sigma, detect, grid=grid, grace=grace)
        size = esc if not math.isnan(esc) else float(min(grid))
        scenarios.append(_variant(base, size).model_copy(update={"split": pool}))
    return Suite(name="adaptive_probe", seed=0, scenarios=tuple(scenarios))


def escape_table(base_scenarios, clean, sigma, detect, *, grid, grace: str = "1h") -> pd.DataFrame:
    """Escape magnitude per (family, variable) for a set of base scenarios."""
    rows = []
    for base in base_scenarios:
        rows.append({
            "family": base.family,
            "variable": base.variable,
            "escape_size": escape_magnitude(base, clean, sigma, detect, grid=grid, grace=grace),
        })
    return pd.DataFrame(rows)
