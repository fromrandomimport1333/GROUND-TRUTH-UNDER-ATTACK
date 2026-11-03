"""Reference baselines: deliberately simple *floors*, not the team detector.

Each function takes one variable's wide table (time x station) and returns a boolean alarm table with the
same index and columns, so it scores through ``eval.run`` like any other detector. They are independent of
each other; further baselines (e.g. a cross-source one) are separate functions in this module.

All persistence / run logic is **causal**: an alarm is raised at the step where the evidence first becomes
sufficient (``persist`` disagreeing steps, 24 unchanged steps, ...) and is never back-filled over earlier
steps, so event latencies are honest.

Everything is vectorised over the whole table with numpy; the only Python loop is over stations (and each
iteration touches a (time x 6) slab). Missing values (NaN) never raise an alarm and break any run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# range / step / constancy limits (plan Task 10)
RANGES = {
    "air_temperature": (-30.0, 45.0),  # degC
    "relative_humidity": (0.0, 100.0),  # %
    "precipitation": (0.0, 30.0),  # mm per 5 min
}
STEP_LIMITS = {"air_temperature": 5.0, "relative_humidity": 20.0}  # |delta| per 5 min, strictly greater
CONSTANCY_STEPS = 24  # unchanged for >= 24 steps (2 h) = 24 consecutive equal steps
PRECIP_HOUR_STEPS = 12  # one hour of 5-min steps
PRECIP_MIN_DIFF_MM = 2.0
PRECIP_MEDIAN_FACTOR = 3.0


# ----------------------------------------------------------------------------- helpers


def _run_length(mask: np.ndarray) -> np.ndarray:
    """Per column, the number of consecutive True values ending at each row (0 where False). Shape kept."""
    csum = np.cumsum(mask, axis=0, dtype=np.int32)
    restart = np.maximum.accumulate(np.where(mask, 0, csum), axis=0)
    return csum - restart


def _row_median(slab: np.ndarray) -> np.ndarray:
    """Median over axis 1 ignoring NaN (NaN where a row has no finite value). ``slab`` is (n, k)."""
    ordered = np.sort(slab, axis=1)  # NaN sorts last
    n_valid = np.count_nonzero(~np.isnan(slab), axis=1)
    lo = np.clip((n_valid - 1) // 2, 0, None)[:, None]
    hi = np.clip(n_valid // 2, 0, None)[:, None]
    low = np.take_along_axis(ordered, lo, axis=1)[:, 0].astype(np.float64)
    high = np.take_along_axis(ordered, hi, axis=1)[:, 0].astype(np.float64)
    median = 0.5 * (low + high)
    median[n_valid == 0] = np.nan
    return median


def _as_frame(alarm: np.ndarray, like: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(alarm.astype(bool, copy=False), index=like.index, columns=like.columns)


def _neighbor_columns(station: str, neighbors: dict[str, list[str]], position: dict[str, int]):
    """(column of ``station``, columns of its neighbours present in the table) or None."""
    j = position.get(station)
    cols = [position[n] for n in neighbors.get(station, []) if n in position and n != station]
    return (j, cols) if j is not None and cols else None


# ----------------------------------------------------------------------------- rules


def rule_baseline(values: pd.DataFrame, variable: str) -> pd.DataFrame:
    """Range, step and constancy checks on each station's own series.

    * range (inclusive bounds are fine): T outside [-30, 45] degC, RH outside [0, 100] %, precipitation
      outside [0, 30] mm per 5 min.
    * step (T, RH): |x[t] - x[t-1]| strictly greater than 5 degC / 20 %, flagged at t.
    * constancy (T, RH): value unchanged over the last 24 steps (2 h), i.e. 25 identical readings, flagged
      from the 25th reading onward. Relative humidity readings of exactly 100 % are exempt (saturation).
    """
    if variable not in RANGES:
        raise ValueError(f"unknown variable {variable!r}; expected one of {sorted(RANGES)}")
    x = values.to_numpy()
    low, high = RANGES[variable]
    alarm = (x < low) | (x > high)  # NaN compares False

    if variable in STEP_LIMITS:
        jump = np.zeros(x.shape, dtype=bool)
        jump[1:] = np.abs(x[1:] - x[:-1]) > STEP_LIMITS[variable]
        alarm |= jump
        same = np.zeros(x.shape, dtype=bool)
        same[1:] = x[1:] == x[:-1]
        if variable == "relative_humidity":
            same &= x != 100
        alarm |= _run_length(same) >= CONSTANCY_STEPS
    return _as_frame(alarm, values)


# ------------------------------------------------------------------------- neighbours


def neighbor_baseline(
    values: pd.DataFrame,
    neighbors: dict[str, list[str]],
    sigma: pd.Series | None,
    z: float = 4.0,
    persist: int = 3,
    *,
    variable: str = "air_temperature",
) -> pd.DataFrame:
    """Disagreement with the median of a station's nearest neighbours.

    ``air_temperature`` / ``relative_humidity``: alarm when ``|x - median(neighbours)| / sigma_ref > z`` has
    held for ``persist`` consecutive steps (raised at the ``persist``-th step, held while it continues).
    ``sigma`` is the per-station sigma_ref of *this* variable (a Series indexed by station); a station with
    a missing or non-positive sigma, or without listed neighbours in the table, never alarms.

    ``variable="precipitation"`` (``sigma`` unused, may be None): see :func:`_neighbor_precipitation`.
    ``variable`` is keyword-only and optional so the call matches the plan's signature for T and RH.
    """
    if persist < 1:
        raise ValueError("persist must be >= 1")
    if variable == "precipitation":
        return _neighbor_precipitation(values, neighbors)
    if sigma is None:
        raise ValueError("sigma is required for air_temperature / relative_humidity")

    x = np.asfortranarray(values.to_numpy())  # column slabs are contiguous
    position = {str(c): i for i, c in enumerate(values.columns)}
    exceed = np.zeros(x.shape, dtype=bool, order="F")
    for station in values.columns:
        found = _neighbor_columns(str(station), neighbors, position)
        scale = sigma.get(station, np.nan)
        if found is None or not scale > 0:  # NaN scale also lands here
            continue
        j, cols = found
        reference = _row_median(x[:, cols])
        exceed[:, j] = np.abs(x[:, j] - reference) > z * scale
    return _as_frame(_run_length(exceed) >= persist, values)


def _neighbor_precipitation(values: pd.DataFrame, neighbors: dict[str, list[str]]) -> pd.DataFrame:
    """Hourly precipitation sum against the neighbours' hourly median.

    The hourly sum is the trailing 12-step (1 h) sum evaluated at every step, defined only when all 12
    values are present. Alarm when ``|S - M| > max(2 mm, 3 * M)`` with ``M`` the median of the neighbours'
    hourly sums (the rule exactly as specified; as written it can only fire on over-reading, because
    ``S >= 0`` gives ``|S - M| <= M <= max(2, 3M)`` whenever the station reads *less* than its neighbours).
    """
    hourly = values.rolling(PRECIP_HOUR_STEPS, min_periods=PRECIP_HOUR_STEPS).sum()
    s = np.asfortranarray(hourly.to_numpy(dtype=np.float32))
    del hourly
    position = {str(c): i for i, c in enumerate(values.columns)}
    alarm = np.zeros(s.shape, dtype=bool, order="F")
    for station in values.columns:
        found = _neighbor_columns(str(station), neighbors, position)
        if found is None:
            continue
        j, cols = found
        reference = _row_median(s[:, cols])
        limit = np.maximum(PRECIP_MIN_DIFF_MM, PRECIP_MEDIAN_FACTOR * reference)
        alarm[:, j] = np.abs(s[:, j] - reference) > limit
    return _as_frame(alarm, values)


def crosssource_baseline(
    values: pd.DataFrame,
    reference: pd.Series,
    sigma_area: float,
    z: float = 4.0,
    persist: int = 6,
) -> pd.DataFrame:
    """Flag a network-wide departure from an independent reference (the remote-sensing angle).

    The network area-mean is compared to ``reference`` (ERA5 or an independent station cluster,
    on the same index). When ``|area_mean - reference| / sigma_area > z`` for ``persist``
    consecutive steps, every station is alarmed -- this is the one check that catches a
    ``network_bias`` fault, which neighbour consistency is blind to because the whole network
    moves together. ``sigma_area`` is the normal scatter of (area_mean - reference).
    """
    area = values.mean(axis=1).to_numpy(dtype=float)
    ref = reference.reindex(values.index).to_numpy(dtype=float)
    exceed = np.abs(area - ref) > z * float(sigma_area)
    fired = _run_length(exceed[:, None])[:, 0] >= persist
    alarm = np.repeat(fired[:, None], values.shape[1], axis=1)
    return _as_frame(alarm, values)
