"""Stamp a placed ``Suite`` onto the clean tables and emit the interval labels table.

``inject`` is the only place where fault windows are written into data, and it never
touches 2023 (the calibration year).  Windows are located on the table's time index
exactly (labels ``end`` is exclusive: the last faulted step is ``end - 5 min``), values
are read from the *clean* tables, and every variable table is copied once and mutated by
integer slices.

Label rows are emitted one per (scenario, affected station).  Everything in the labels
that is a time is tz-aware UTC.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from attacks.families import CONTEXT_FAMILIES, apply_fault, clip_physical, delta_onset
from attacks.schema import Scenario, Suite
from data_prep.wegenernet import UNITS

HIST_STEPS = 30 * 288  # realistic_replace draws on the station's prior 30 days
DAY_STEPS = 288
CLEAN_DIR = Path(__file__).resolve().parents[1] / "data" / "clean"

LABEL_COLUMNS = [
    "scenario_id",
    "suite",
    "family",
    "category",
    "variable",
    "station",
    "start",
    "end",
    "onset",
    "size",
    "size_phys",
    "unit",
    "duration_h",
    "n_stations",
    "split",
]

CATEGORY = "A"  # value faults; record faults (B/C) are labelled elsewhere
FAULT_FLOOR = pd.Timestamp("2024-01-01", tz="UTC")  # 2023 is the fault-free calibration year
DEFAULT_NEIGHBORS = Path(__file__).resolve().parents[1] / "data" / "clean" / "neighbors.csv"

# The one size parameter of each family (a family has at most one of these).
_SIZE_KEYS = ("k", "f", "g", "total_mm", "w_steps")
# Float tolerance when asking "has the injected delta reached 1 sigma?": a drift with
# k = 1 reaches sigma exactly on its last step and must not lose it to rounding.
_ONSET_RTOL = 1e-9
_HOUR = pd.Timedelta(hours=1)


def _utc(ts, what: str) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware, got naive {ts}")
    return ts.tz_convert("UTC")


def _check_index(variable: str, index: pd.Index) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError(f"clean[{variable!r}] needs a tz-aware UTC DatetimeIndex")
    if str(index.tz) != "UTC":
        raise ValueError(f"clean[{variable!r}] index must be UTC, got {index.tz}")
    if len(index) < 2 or not index.is_monotonic_increasing:
        raise ValueError(f"clean[{variable!r}] needs a sorted index with >= 2 rows")
    return index


def _locate(sc: Scenario, index: pd.DatetimeIndex) -> tuple[int, int, pd.Timestamp, pd.Timestamp]:
    """Integer window [i0, i1) of ``sc`` on ``index``; ``end`` is exclusive."""
    start, end = _utc(sc.start, f"{sc.id} start"), _utc(sc.end, f"{sc.id} end")
    if start < FAULT_FLOOR:
        raise ValueError(
            f"{sc.id}: window starts {start} before {FAULT_FLOOR}; 2023 is the fault-free "
            "calibration year and is never modified"
        )
    step = index[1] - index[0]
    i0 = int(index.searchsorted(start, side="left"))
    i1 = int(index.searchsorted(end, side="left"))
    start_ok = i0 < len(index) and index[i0] == start
    end_ok = (i1 < len(index) and index[i1] == end) or (
        i1 == len(index) and end == index[-1] + step  # window runs to the last row
    )
    if not (start_ok and end_ok):
        raise ValueError(
            f"{sc.id}: window [{start}, {end}) is not on the table's time grid "
            f"({step} steps, {index[0]} .. {index[-1]})"
        )
    return i0, i1, start, end


def _load_neighbors() -> pd.DataFrame:
    return pd.read_csv(DEFAULT_NEIGHBORS, dtype={"station": str, "neighbor": str})


def _load_diurnal(variable: str) -> pd.DataFrame:
    path = CLEAN_DIR / f"diurnal_{variable}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"cycle_shaped needs {path}; build it with data_prep.build or pass diurnal=..."
        )
    tbl = pd.read_parquet(path)
    tbl.columns = tbl.columns.astype(str)
    return tbl


def _tod_bins(index: pd.DatetimeIndex) -> np.ndarray:
    return (index.hour.to_numpy() * 12 + index.minute.to_numpy() // 5).astype(int)


def _neighbor_lists(neighbors: pd.DataFrame) -> dict[str, list[str]]:
    """station -> its neighbours ordered by rank (nearest first)."""
    table = neighbors.assign(
        station=neighbors["station"].astype(str), neighbor=neighbors["neighbor"].astype(str)
    ).sort_values(["station", "rank"], kind="stable")
    return {s: g["neighbor"].tolist() for s, g in table.groupby("station", sort=False)}


def _affected_stations(sc: Scenario, neighbors: dict[str, list[str]] | None) -> list[str]:
    """The stations a scenario writes to, target first.

    Only ``multi_offset`` fans out: it must cover ``n`` stations.  A placed scenario that
    already lists all ``n`` stations is used as is; one that lists just the target (e.g. the
    hand-written showcase) is completed with the target's nearest neighbours by rank.
    """
    stations = [str(s) for s in sc.stations]
    if not stations:
        raise ValueError(f"{sc.id}: scenario has no stations")
    if sc.family != "multi_offset":
        return stations

    n = int(sc.params["n"])
    if len(stations) > n:
        raise ValueError(f"{sc.id}: lists {len(stations)} stations but multi_offset n={n}")
    if len(stations) == n:
        return stations

    extra = [s for s in (neighbors or {}).get(stations[0], []) if s not in stations]
    need = n - len(stations)
    if len(extra) < need:
        raise ValueError(
            f"{sc.id}: multi_offset n={n} needs {need} nearest neighbour(s) of station "
            f"{stations[0]!r} but neighbors table lists only {len(extra)}; list all {n} "
            "stations in the scenario instead"
        )
    return stations + extra[:need]


def _label_frame(rows: list[dict], unit: str) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=LABEL_COLUMNS)
    for col in ("start", "end", "onset"):
        frame[col] = pd.to_datetime(frame[col], utc=True).dt.as_unit(unit)
    for col in ("size", "size_phys", "duration_h"):
        frame[col] = frame[col].astype("float64")
    frame["n_stations"] = frame["n_stations"].astype("int64")
    return frame


def inject(
    suite: Suite,
    clean: dict[str, pd.DataFrame],
    sigma: pd.DataFrame,
    *,
    neighbors: pd.DataFrame | None = None,
    diurnal: dict[str, pd.DataFrame] | None = None,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Return ``(faulted tables, labels)`` for ``suite`` stamped onto ``clean``.

    ``clean`` maps variable -> (time x station) table; every returned table is a copy.
    ``sigma`` is indexed by station with one column per variable (precipitation uses no
    sigma).  ``neighbors`` (columns ``station, rank, neighbor``) is only read to complete a
    ``multi_offset`` scenario that lists fewer than ``n`` stations; by default it is loaded
    from ``data/clean/neighbors.csv`` when needed.

    Scenario ``i`` draws from ``np.random.default_rng([suite.seed, i])``.  A ``stuck`` window
    whose preceding clean value is not finite cannot hold anything: it is skipped with a
    ``RuntimeWarning`` and gets no label row.
    """
    sources: dict[str, np.ndarray] = {}
    outputs: dict[str, np.ndarray] = {}
    positions: dict[str, dict[str, int]] = {}
    sigma_maps: dict[str, dict[str, float]] = {}
    claimed: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
    neighbor_lists: dict[str, list[str]] | None = None
    rows: list[dict] = []

    for variable, table in clean.items():
        _check_index(variable, table.index)

    unit = next((t.index.unit for t in clean.values()), "us")

    for scenario_index, sc in enumerate(suite.scenarios):
        variable = sc.variable
        if variable not in clean:
            raise KeyError(f"{sc.id}: variable {variable!r} is not in the clean tables")
        table = clean[variable]
        if variable not in sources:
            sources[variable] = table.to_numpy()
            positions[variable] = {str(c): j for j, c in enumerate(table.columns)}
            if variable in sigma.columns:
                sigma_maps[variable] = {str(s): float(v) for s, v in sigma[variable].items()}
        i0, i1, start, end = _locate(sc, table.index)

        if sc.family == "multi_offset" and len(sc.stations) < int(sc.params["n"]):
            if neighbors is None:
                neighbors = _load_neighbors()
            if neighbor_lists is None:
                neighbor_lists = _neighbor_lists(neighbors)
        stations = _affected_stations(sc, neighbor_lists)

        rng = np.random.default_rng([suite.seed, scenario_index])
        src = sources[variable]
        sigma_based = "k" in sc.params
        size = next((float(sc.params[key]) for key in _SIZE_KEYS if key in sc.params), math.nan)
        onset_by_station: dict[str, pd.Timestamp] = {}
        applied: list[tuple[str, float]] = []

        for station in stations:
            try:
                j = positions[variable][station]
            except KeyError:
                raise KeyError(f"{sc.id}: station {station!r} not in clean[{variable!r}]") from None

            for a, b, other in claimed.get((variable, station), []):
                if i0 < b and a < i1:
                    raise ValueError(
                        f"{sc.id}: window overlaps {other} on {variable} station {station}; "
                        "labels would be ambiguous"
                    )

            x = src[i0:i1, j].astype(np.float64)
            prev = float(src[i0 - 1, j]) if i0 > 0 else math.nan
            if sc.family == "stuck" and not math.isfinite(prev):
                warnings.warn(
                    f"{sc.id}: skipped station {station}: the clean value just before the "
                    "window is not finite, so stuck has nothing to hold",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue

            if variable == "precipitation":
                sig = 1.0  # rain families ignore sigma
            else:
                sig = sigma_maps.get(variable, {}).get(station, math.nan)
                if sigma_based and not math.isfinite(sig):
                    raise ValueError(f"{sc.id}: no finite sigma_ref for {variable} station {station}")

            context = None
            if sc.family in CONTEXT_FAMILIES:
                tod = _tod_bins(table.index[i0:i1])
                if sc.family in ("realistic_replace", "value_replay"):
                    context = {"history": src[max(0, i0 - HIST_STEPS):i0, j].astype(np.float64),
                               "tod_index": tod}
                else:  # cycle_shaped
                    if diurnal is None or variable not in diurnal:
                        diurnal = {**(diurnal or {}), variable: _load_diurnal(variable)}
                    context = {"diurnal": diurnal[variable][station].to_numpy()[tod % DAY_STEPS]}

            # float64 end to end; clip before measuring the effective injected delta
            faulted = clip_physical(
                variable, apply_fault(sc.family, x, sc.params, sig, rng, prev, context=context)
            )
            idx_onset = delta_onset(sc.family, faulted - x, sig * (1.0 - _ONSET_RTOL))

            if variable not in outputs:
                outputs[variable] = table.to_numpy(copy=True)  # the one copy of this table
            outputs[variable][i0:i1, j] = faulted
            claimed.setdefault((variable, station), []).append((i0, i1, sc.id))

            onset_by_station[station] = (
                start + idx_onset * (table.index[1] - table.index[0])
                if idx_onset < i1 - i0
                else pd.NaT  # drift never reached 1 sigma: onset undefined
            )
            applied.append((station, sig))

        for station, sig in applied:
            rows.append(
                {
                    "scenario_id": sc.id,
                    "suite": suite.name,
                    "family": sc.family,
                    "category": CATEGORY,
                    "variable": variable,
                    "station": station,
                    "start": start,
                    "end": end,
                    "onset": onset_by_station[station],
                    "size": size,
                    "size_phys": size * sig if sigma_based else size,
                    "unit": UNITS[variable],
                    "duration_h": (end - start) / _HOUR,
                    "n_stations": len(applied),
                    "split": sc.split,
                }
            )

    faulted_tables = {
        v: (
            pd.DataFrame(outputs[v], index=t.index, columns=t.columns)
            if v in outputs
            else t.copy()
        )
        for v, t in clean.items()
    }
    return faulted_tables, _label_frame(rows, unit)
