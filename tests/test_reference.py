"""Cross-source reference + network-wide bias: only an external reference catches it."""

import numpy as np
import pandas as pd

from data_prep.reference import align_to_grid, complementary_reference
from eval.baselines import crosssource_baseline, neighbor_baseline


def _grid(hours=48):
    return pd.date_range("2024-06-01", periods=hours * 12, freq="5min", tz="UTC", name="time")


def test_reference_aligned_to_5min_grid():
    hourly = pd.Series(
        np.arange(49, dtype=float),
        index=pd.date_range("2024-06-01", periods=49, freq="1h", tz="UTC"),
    )
    out = align_to_grid(hourly, _grid(48))
    assert out.index.equals(_grid(48))
    assert not out.isna().any()
    assert out.iloc[0] == 0.0  # first hour value held across its 5-min steps


def test_neighbor_baseline_misses_network_bias():
    idx = _grid(48)
    stations = [str(i) for i in range(1, 7)]
    rng = np.random.default_rng(0)
    base = 15 + 5 * np.sin(2 * np.pi * np.arange(len(idx)) / 288)
    clean = pd.DataFrame(
        {s: base + rng.normal(0, 0.3, len(idx)) for s in stations}, index=idx
    ).astype("float32")
    neighbors = {s: [t for t in stations if t != s][:5] for s in stations}
    sigma = pd.Series(0.5, index=stations)
    # shift EVERY station by +3 sigma over the second half: neighbours still agree
    attacked = clean.copy()
    half = len(idx) // 2
    attacked.iloc[half:] += 1.5  # 3 * 0.5
    alarms = neighbor_baseline(attacked, neighbors, sigma, z=4.0, persist=3)
    assert alarms.iloc[half:].to_numpy().mean() < 0.02  # neighbour consistency is blind to it


def test_crosssource_baseline_catches_network_bias():
    idx = _grid(48)
    stations = [str(i) for i in range(1, 7)]
    base = pd.Series(15 + 5 * np.sin(2 * np.pi * np.arange(len(idx)) / 288), index=idx)
    clean = pd.DataFrame({s: base.to_numpy() for s in stations}, index=idx).astype("float32")
    reference = base.copy()  # an independent source that did NOT move
    half = len(idx) // 2
    attacked = clean.copy()
    attacked.iloc[half:] += 1.5  # whole network shifted +3 sigma_area
    alarms = crosssource_baseline(attacked, reference, sigma_area=0.2, z=4.0, persist=6)
    assert alarms.iloc[:half].to_numpy().mean() < 0.01  # quiet before
    assert alarms.iloc[half + 6 :].to_numpy().all()      # fires across all stations after


def test_complementary_reference_is_untouched_pool_mean():
    idx = _grid(24)
    clean = pd.DataFrame(
        {"1": np.full(len(idx), 10.0), "2": np.full(len(idx), 12.0), "3": np.full(len(idx), 20.0)},
        index=idx,
    ).astype("float32")
    ref = complementary_reference(clean, reference_stations=["1", "2"])
    assert np.allclose(ref.to_numpy(), 11.0)  # mean of stations 1 and 2, independent of station 3
