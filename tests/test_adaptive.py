"""Escape-magnitude evaluation: the smallest fault a detector fails to catch."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from attacks.adaptive import build_adaptive_suite, escape_magnitude
from attacks.schema import Scenario

IDX = pd.date_range("2023-12-31", "2024-01-04", freq="5min", tz="UTC", inclusive="left", name="time")
STATIONS = ["1", "2", "3", "4", "5"]
SIGMA = pd.DataFrame(
    {"air_temperature": [0.5] * 5, "relative_humidity": [2.0] * 5},
    index=pd.Index(STATIONS, name="station"),
)
T0 = datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc)


def clean():
    n = len(IDX)
    data = {s: np.full(n, 10.0, np.float32) for s in STATIONS}  # flat, so the detector sees only the fault
    return {
        "air_temperature": pd.DataFrame(data, index=IDX),
        "relative_humidity": pd.DataFrame({s: np.full(n, 60.0, np.float32) for s in STATIONS}, index=IDX),
        "precipitation": pd.DataFrame({s: np.zeros(n, np.float32) for s in STATIONS}, index=IDX),
    }


def threshold_detector(z=2.0):
    """Alarms a cell when it departs from its column's median by more than z * 0.5 degC."""
    def detect(values):
        out = {}
        for var, df in values.items():
            med = df.median(axis=0)
            out[var] = (df - med).abs() > z * 0.5
        return out
    return detect


def offset_scenario(k):
    return Scenario(
        id="probe-offset-ai-0000", family="offset", variable="air_temperature",
        stations=("1",), start=T0, end=T0 + timedelta(hours=24), params={"k": k}, split="heldout",
    )


def test_escape_magnitude_just_below_threshold():
    # detector trips at k > 2; grid straddles it -> largest undetected size is 2.0
    esc = escape_magnitude(offset_scenario(1.0), clean(), SIGMA, threshold_detector(2.0),
                           grid=[0.5, 1.0, 2.0, 3.0, 5.0])
    assert esc == 2.0


def test_escape_is_nan_when_smallest_caught():
    esc = escape_magnitude(offset_scenario(1.0), clean(), SIGMA, threshold_detector(0.4),
                           grid=[1.0, 2.0, 3.0])
    assert np.isnan(esc)


def test_adaptive_suite_has_one_scenario_per_base_family():
    bases = [offset_scenario(1.0),
             offset_scenario(1.0).model_copy(update={"family": "noise", "id": "probe-noise-ai-0001"})]
    suite = build_adaptive_suite(bases, clean(), SIGMA, threshold_detector(2.0),
                                 grid=[0.5, 1.0, 2.0, 3.0, 5.0])
    assert {s.family for s in suite.scenarios} == {"offset", "noise"}


def test_adaptive_suite_handles_sizeless_family():
    # flatline has no size parameter to sweep; the suite keeps it rather than crashing
    flat = offset_scenario(1.0).model_copy(
        update={"family": "flatline", "id": "probe-flatline-ai-0002", "params": {"w_steps": 36.0}}
    )
    suite = build_adaptive_suite([flat], clean(), SIGMA, threshold_detector(2.0), grid=[1.0, 2.0])
    assert [s.family for s in suite.scenarios] == ["flatline"]


def test_adaptive_suite_is_deterministic():
    bases = [offset_scenario(1.0)]
    a = build_adaptive_suite(bases, clean(), SIGMA, threshold_detector(2.0), grid=[1.0, 2.0, 3.0])
    b = build_adaptive_suite(bases, clean(), SIGMA, threshold_detector(2.0), grid=[1.0, 2.0, 3.0])
    assert [s.params for s in a.scenarios] == [s.params for s in b.scenarios]
