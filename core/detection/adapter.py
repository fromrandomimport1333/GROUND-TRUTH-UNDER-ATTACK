"""Layer 2 -> Layer 3 adapter: FeatureWindow stream (or compact CSV export) -> detector ``StreamData``.

Layer 2 emits one ``FeatureWindow`` per (cell, 30-minute tick). The detector does not consume the
Layer 2 features; it recomputes its own causal features from the raw series, so this adapter only
recovers, per tick and cell, the three raw values and their quality flags:

* **values**: ``ProcessedReading.normalized_value`` of the window's *central* cell readings. Readings
  that Layer 1/2 rejected (bad signature, replay, gap, unknown unit, ...) carry no normalized value
  and become NaN, so the detector never scores a value Layer 1 did not accept. Kelvin -> degC.
* **flags**: the detector treats ``flag == 0`` as clean and uses ``flag/100`` as a feature. A window
  only carries the binary ``<quantity>__flagged_observation_count``, so 0 -> 0 % and 1 -> NaN
  (unknown percentage, point not clean). The compact CSV has the exact ``__raw_flag_percent``;
  prefer :func:`from_compact_csv` when it is available.
* **sites**: Layer 2 cell ``wn_grid_y{Y}_x{X}`` matches detector site ``grid_y{YYY}_x{XXX}`` by grid
  index. Output follows the *detector's* site order; detector sites absent from the input are NaN.

Validity caveats (checked and reported, never silently fixed):

* The frozen detector was trained on ``T_z300m_HHM`` (``config/detector.yaml``, ``data.variables``);
  Layer 2 currently exports ``T_zAvgTerr_HHM``. :func:`check_compatibility` reports the mismatch.
* The detector's spatial references use neighbour rings over its full site grid; with only a few
  covered sites most rings are empty and spatial features degrade. ``coverage`` reports this.
"""
from __future__ import annotations

import csv
import gzip
import re
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from core.detection.data import StreamData, episodes
from core.detection.utils import VARIABLES, config

INTERVAL = np.timedelta64(30, "m")
KELVIN = 273.15
# Detector variable -> Layer 2 quantity name (half-hourly export) and its normalized unit.
DEFAULT_QUANTITIES = {
    "temperature_c": "T_zAvgTerr_HHM",
    "relative_humidity_pct": "RH_HHM",
    "precip_mm_30min": "Precip_HHS",
}
_CELL = re.compile(r"^(?:wn_)?grid_y0*(\d+)_x0*(\d+)$")


def grid_index(site_id):
    """``(y, x)`` grid indices of a Layer 2 cell id or a detector site id."""
    match = _CELL.match(site_id)
    if not match:
        raise ValueError(f"Unrecognised cell/site id: {site_id!r}")
    return int(match.group(1)), int(match.group(2))


def check_compatibility(quantities=None):
    """List the differences between the Layer 2 quantities and the detector's training source."""
    quantities = {**DEFAULT_QUANTITIES, **(quantities or {})}
    trained = {name: spec[0] for name, spec in config("data")["variables"].items()}
    return [f"{v}: detector trained on {trained[v]}, Layer 2 supplies {quantities[v]}"
            for v in VARIABLES if trained.get(v) != quantities[v]]


def _to_detector_units(variable, value, unit):
    if value is None:
        return np.nan
    if variable == "temperature_c":
        if unit == "K":
            return value - KELVIN
        if unit not in ("C", "°C", "degC"):
            raise ValueError(f"Unsupported temperature unit {unit!r}")
    elif variable == "relative_humidity_pct" and unit not in ("%", "percent"):
        raise ValueError(f"Unsupported humidity unit {unit!r}")
    elif variable == "precip_mm_30min" and unit not in ("mm", "mm/30min"):
        raise ValueError(f"Unsupported precipitation unit {unit!r}")
    return float(value)


@dataclass
class Assembly:
    """Ticks collected so far: ``{time: {cell_id: (values[3], flags[3])}}`` with window de-duplication."""
    quantities: dict = field(default_factory=lambda: dict(DEFAULT_QUANTITIES))
    ticks: dict = field(default_factory=lambda: defaultdict(dict))
    seen: set = field(default_factory=set)

    def add(self, time, cell, values, flags, window_id=None):
        if window_id is not None:
            if window_id in self.seen:
                return False  # WindowPipeline is at-least-once: duplicates are dropped
            self.seen.add(window_id)
        if cell in self.ticks[time]:
            raise ValueError(f"Two different windows for {cell} at {time}")
        self.ticks[time][cell] = (np.asarray(values, dtype=np.float32), np.asarray(flags, dtype=np.float32))
        return True

    def stream(self, sites, *, min_coverage=1):
        """Build ``StreamData`` on the detector's ``sites`` (``site_id``/``easting_m``/``northing_m``).

        Ticks must be contiguous at 30 minutes; a gap raises (the detector requires a regular grid and
        resets history only at episode boundaries). Returns ``(StreamData, coverage)``.
        """
        if not self.ticks:
            raise ValueError("No windows collected")
        times = np.array(sorted(self.ticks), dtype="datetime64[ns]")
        if len(times) > 1 and np.any(np.diff(times) != INTERVAL):
            gaps = times[1:][np.diff(times) != INTERVAL]
            raise ValueError(f"Non-contiguous ticks (first gap before {gaps[0]}); split into separate streams")
        sites = sites.reset_index(drop=True)
        index = {grid_index(s): i for i, s in enumerate(sites.site_id)}
        values = np.full((len(times), len(sites), 3), np.nan, dtype=np.float32)
        flags = np.full_like(values, np.nan)
        covered, ignored = set(), set()
        for t, key in enumerate(sorted(self.ticks)):
            for cell, (v, q) in self.ticks[key].items():
                i = index.get(grid_index(cell))
                if i is None:
                    ignored.add(cell)
                    continue
                covered.add(i)
                values[t, i], flags[t, i] = v, q
        coverage = {"detector_sites": len(sites), "covered_sites": len(covered),
                    "input_cells_not_in_detector_grid": sorted(ignored), "ticks": len(times),
                    "variable_mismatches": check_compatibility(self.quantities)}
        if len(covered) < min_coverage:
            raise ValueError(f"Only {len(covered)} detector sites covered by the input cells "
                             f"({len(ignored)} input cells are off the detector grid)")
        return StreamData(times, times - INTERVAL, values, flags, sites), coverage


def _key(time):
    return np.datetime64(pd.Timestamp(time).tz_convert("UTC").tz_localize(None), "ns")


def window_record(window, quantities=None):
    """Extract ``(time, cell_id, values[3], flags[3])`` for the central cell of one FeatureWindow."""
    quantities = {**DEFAULT_QUANTITIES, **(quantities or {})}
    flag_features = {f.name: f.value for f in window.temporal_features.values}
    cells = {f.sensor_id for f in window.temporal_features.values if f.sensor_id}
    if len(cells) != 1:
        raise ValueError(f"Window {window.window_id}: expected one central cell, found {sorted(cells)}")
    cell = cells.pop()
    by_quantity = {}
    for item in window.readings:
        raw = item.provenance.reading
        if raw.sensor_id == cell:
            by_quantity[raw.quantity] = item
    values, flags = [], []
    for variable in VARIABLES:
        quantity = quantities[variable]
        item = by_quantity.get(quantity)
        if item is not None and item.ready_for_analysis:
            values.append(_to_detector_units(variable, item.normalized_value, item.normalized_unit))
        else:
            values.append(np.nan)
        flagged = flag_features.get(f"{quantity}__flagged_observation_count")
        flags.append(0.0 if flagged == 0.0 and np.isfinite(values[-1]) else np.nan)
    return _key(window.start), cell, values, flags


def from_windows(windows, sites, quantities=None, *, min_coverage=1):
    """Assemble an iterable of FeatureWindow objects into ``(StreamData, coverage)`` on ``sites``.

    Layer 2 windows are ``[label, label+30min)``, so ``window.start`` is the source interval-end label,
    which is exactly the detector's ``times`` convention (``starts = times - 30 min``).
    """
    assembly = Assembly({**DEFAULT_QUANTITIES, **(quantities or {})})
    for window in windows:
        time, cell, values, flags = window_record(window, assembly.quantities)
        assembly.add(time, cell, values, flags, window.window_id)
    return assembly.stream(sites, min_coverage=min_coverage)


def read_windows_jsonl(paths):
    """Yield FeatureWindow objects from Layer 2 ``windows.jsonl.gz`` partitions (in the given order)."""
    from core.contracts import FeatureWindow
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield FeatureWindow.model_validate_json(line)


def from_compact_csv(paths, sites, quantities=None, *, min_coverage=1):
    """Assemble Layer 2 ``compact.csv.gz`` partitions into ``(StreamData, coverage)``.

    Uses ``<quantity>__eligible`` (Layer 1/2 acceptance), ``<quantity>__latest`` (value in source
    units) and the exact ``<quantity>__raw_flag_percent``. Empty fields are nulls.
    """
    quantities = {**DEFAULT_QUANTITIES, **(quantities or {})}
    units = {"temperature_c": "K", "relative_humidity_pct": "%", "precip_mm_30min": "mm"}
    assembly = Assembly(quantities)
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                values, flags = [], []
                for variable in VARIABLES:
                    q = quantities[variable]
                    eligible = row.get(f"{q}__eligible") == "True"
                    if f"{q}__latest" not in row:
                        raise ValueError(f"{path}: column {q}__latest missing")
                    latest = row[f"{q}__latest"] or None
                    value = _to_detector_units(variable, float(latest), units[variable]) if eligible and latest else np.nan
                    flag = row.get(f"{q}__raw_flag_percent") or None
                    values.append(value)
                    # Fill value 101 (and anything outside 0..100) = unknown, as in data.extract.
                    f = float(flag) if flag is not None else np.nan
                    flags.append(f if np.isfinite(value) and 0 <= f <= 100 else np.nan)
                assembly.add(_key(row["timestamp"]), row["cell_id"], values, flags, row.get("window_id"))
    return assembly.stream(sites, min_coverage=min_coverage)


def score_stream(detector, data):
    """Run a frozen ``Detector`` over a stream in replay episodes.

    Returns a long DataFrame (one row per scored point) with time, site, variable, p-value and alert.
    The first ``warmup_steps`` of every episode are never scored (detector convention).
    """
    cfg = config("features")
    rows = []
    for _, episode in episodes(data, cfg["episode_steps"], cfg["warmup_steps"]):
        p, alert = detector(episode)
        t, s, v = np.nonzero(np.isfinite(p))
        rows.append(pd.DataFrame({"time": episode.times[t], "site_id": episode.sites.site_id.to_numpy()[s],
                                  "variable": np.asarray(VARIABLES)[v], "p_value": p[t, s, v],
                                  "alert": alert[t, s, v]}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["time", "site_id", "variable", "p_value", "alert"])


class Layer3Sink:
    """Callable Layer 3 consumer for ``core.pipeline.WindowPipeline``.

    Buffers windows (deduplicating ``window_id``); call :meth:`flush` to assemble and score. The
    detector needs >= ``warmup_steps`` (48) contiguous ticks before any point can be scored.
    """

    def __init__(self, detector, quantities=None):
        self.detector = detector
        self.assembly = Assembly({**DEFAULT_QUANTITIES, **(quantities or {})})
        problems = check_compatibility(self.assembly.quantities)
        if problems:
            warnings.warn("Layer 2 / detector variable mismatch: " + "; ".join(problems), stacklevel=2)

    def __call__(self, window):
        time, cell, values, flags = window_record(window, self.assembly.quantities)
        self.assembly.add(time, cell, values, flags, window.window_id)

    def flush(self):
        """Score everything buffered and clear the buffer. Returns ``(scores, coverage)``."""
        data, coverage = self.assembly.stream(self.detector.extractor.sites)
        self.assembly = Assembly(self.assembly.quantities)
        return score_stream(self.detector, data), coverage
