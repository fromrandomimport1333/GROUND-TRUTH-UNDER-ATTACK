"""Placement: choose stations and windows for unplaced scenario specs (spec section 6).

``attacks.grid.expand`` yields *unplaced* specs ``{family, variable, params, duration_steps}``.
``place`` turns each of them into a ``Scenario`` by drawing a target station from a pool and a
window start on the 2024 5-minute grid, subject to the rules below.  Nothing here touches data;
the injector (``attacks.injector``) stamps the placed scenarios onto the clean tables.

Rules (all enforced, see ``tests/test_placement.py``)
-----------------------------------------------------
* Windows lie on the 5-minute grid, fully inside the context's 2024 index; ``end`` is exclusive.
* **Gap fill.**  At most 5 % of the window's points at *every* station the scenario writes to may
  be gap-filled (DP flag > 0).  Points whose clean value is not finite also count as unusable.
* **Season.**  Precipitation windows lie entirely in months 4-10 (provider gap-filling makes the
  winter rain record unreliable).  ``ctx.months`` can narrow every variable further (smoke: June).
* **Rain.**  ``rain_scale`` / ``rain_suppress`` windows hold >= 2 mm / >= 5 mm at the target.
  ``rain_inject`` needs the target *and* its 5 nearest neighbours dry (exactly 0) over the window.
* **Conflicts.**  Per variable, a new window is refused if some station of an already placed
  scenario is within 3 km of one of its stations (same station = 0 km) and the two windows are
  less than 24 h apart (or overlap).  This also guarantees the injector never sees two windows
  overlapping at a shared station.
* **Pools.**  A spec draws its stations from one pool only (``dev`` or ``heldout`` ...).
  ``multi_offset`` takes the target plus its ``n - 1`` nearest neighbours *from that same pool*
  and lists all ``n`` stations; every one of them must be gap-eligible.
* **Stuck.**  The row immediately before the window needs a finite clean value, or the injector
  has nothing to hold.
* Station ``"18"`` is never used; station ``"156"`` is never used for precipitation (no gauge).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from pydantic import ValidationError

from attacks.schema import RAIN_FAMILIES, REQUIRED_PARAMS, Scenario
from data_prep.stations import nearest_neighbors

MAX_GAP_SHARE = 0.05
PRECIP = "precipitation"
PRECIP_MONTHS: frozenset[int] = frozenset(range(4, 11))  # April .. October
CONFLICT_KM = 3.0
CONFLICT_PAD = pd.Timedelta(hours=24)
# Minimum rain (mm) inside the window at the target.
RAIN_MIN_MM: dict[str, float] = {"rain_scale": 2.0, "rain_suppress": 5.0}
RAIN_INJECT_NEIGHBORS = 5
_RAIN_TOL = 1e-4  # float32 rain sums: 2.0 mm must not fail as 1.9999999

EXCLUDED_ALL: frozenset[str] = frozenset({"18"})  # 100 % interpolated, decommissioned
EXCLUDED_BY_VARIABLE: dict[str, frozenset[str]] = {PRECIP: frozenset({"156"})}  # no rain gauge


class PlacementError(RuntimeError):
    """A spec could not be placed; the message names the spec."""


# --------------------------------------------------------------------------- split / eligibility


def _station_key(s: str) -> tuple[int, int, str]:
    return (0, int(s), s) if s.isdigit() else (1, 0, s)


def split_stations(
    stations: list[str], seed: int = 2026, dev_share: float = 0.7
) -> dict[str, list[str]]:
    """Deterministic dev / held-out station split (seeded shuffle of the sorted ids).

    The result does not depend on the order of ``stations``.  Both lists come back sorted
    (numeric ids numerically), ``round(dev_share * n)`` stations go to ``"dev"``.
    """
    if not 0.0 < dev_share < 1.0:
        raise ValueError(f"dev_share must be strictly between 0 and 1, got {dev_share}")
    ordered = sorted({str(s) for s in stations}, key=_station_key)
    n_dev = round(dev_share * len(ordered))
    perm = np.random.default_rng(seed).permutation(len(ordered))
    dev = sorted((ordered[i] for i in perm[:n_dev]), key=_station_key)
    heldout = sorted((ordered[i] for i in perm[n_dev:]), key=_station_key)
    return {"dev": dev, "heldout": heldout}


def _max_bad(n: int, max_gap_share: float) -> int:
    """Largest number of gap-filled points a window of ``n`` points may contain."""
    return int(math.floor(max_gap_share * n + 1e-9))


def eligible(
    flags: pd.Series, start_idx: int, n: int, max_gap_share: float = MAX_GAP_SHARE
) -> bool:
    """True if the ``n``-point window at row ``start_idx`` has <= ``max_gap_share`` flag > 0 points."""
    if n <= 0 or start_idx < 0 or start_idx + n > len(flags):
        return False
    window = flags.iloc[start_idx : start_idx + n].to_numpy()
    return int(np.count_nonzero(window > 0)) <= _max_bad(n, max_gap_share)


# --------------------------------------------------------------------------- context


@dataclass
class PlacementContext:
    """Everything ``place`` needs to know about the data (no fault logic lives here).

    flags      variable -> int8 DP-flag table (time x station).  Must cover ``index``.
    values     variable -> clean value table (time x station).  ``precipitation`` is required for
               the rain families; ``air_temperature`` / ``relative_humidity`` are required for
               ``stuck`` (the row before the window must be finite).  If the table also holds the
               last row before ``index[0]`` (2023-12-31 23:55) a ``stuck`` window may start on the
               first row, otherwise that start is refused.  Full 2023-2024 tables are fine.
    dist       station x station distance in km (``data_prep.stations.distance_km``).
    pools      pool name -> station ids, e.g. ``{"dev": [...], "heldout": [...]}``.
    index      the 2024 5-minute UTC index; all windows are placed inside it.
    split      scenario split label and default pool name (``Scenario.split``, id prefix).
    pool       pool name when it differs from ``split`` (smoke draws from the ``dev`` pool).
    months     optional extra restriction of window months for *every* variable.
    neighbors  optional precomputed 5-NN per station for the ``rain_inject`` dryness check
               (used verbatim); by default derived from ``dist`` with excluded stations removed.

    A spec may override ``split`` / ``pool`` with ``spec["split"]`` / ``spec["pool"]``.
    """

    flags: dict[str, pd.DataFrame]
    values: dict[str, pd.DataFrame]
    dist: pd.DataFrame
    pools: dict[str, list[str]]
    index: pd.DatetimeIndex
    split: str = "dev"
    pool: str | None = None
    months: tuple[int, ...] | None = None
    neighbors: dict[str, list[str]] | None = None

    def __post_init__(self) -> None:
        idx = self.index
        if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or str(idx.tz) != "UTC":
            raise ValueError("PlacementContext.index must be a tz-aware UTC DatetimeIndex")
        if len(idx) < 2:
            raise ValueError("PlacementContext.index needs at least 2 rows")
        steps = np.diff(idx.asi8)
        if not (steps == steps[0]).all() or steps[0] <= 0:
            raise ValueError("PlacementContext.index must be regular and increasing")
        if (idx.year != 2024).any():
            raise ValueError("PlacementContext.index must lie inside 2024 (2023 is never faulted)")
        if self.months is not None and (
            not self.months or any(not 1 <= int(m) <= 12 for m in self.months)
        ):
            raise ValueError(f"months must be a non-empty subset of 1..12, got {self.months}")


# --------------------------------------------------------------------------- vector helpers


def _window_counts(row: np.ndarray, n: int) -> np.ndarray:
    """For each start ``s`` in ``0 .. len(row) - n``: how many ``row`` points are set in ``[s, s+n)``."""
    c = np.zeros(row.size + 1, dtype=np.int32)
    np.cumsum(row, dtype=np.int32, out=c[1:])
    return c[n:] - c[:-n]


def _window_sums(row: np.ndarray, n: int) -> np.ndarray:
    c = np.zeros(row.size + 1, dtype=np.float64)
    np.cumsum(row, dtype=np.float64, out=c[1:])
    return c[n:] - c[:-n]


class _VarTables:
    """One variable's tables as (station, time) numpy arrays for fast per-station slicing."""

    __slots__ = ("pos", "bad", "prev_finite", "prec")

    def __init__(self, pos, bad, prev_finite, prec):
        self.pos: dict[str, int] = pos
        self.bad: np.ndarray = bad  # flag > 0, or value not finite
        self.prev_finite: np.ndarray | None = prev_finite  # clean value at row i-1 is finite
        self.prec: np.ndarray | None = prec  # precipitation values (NaN kept)


# --------------------------------------------------------------------------- placer


class _Placer:
    def __init__(self, ctx: PlacementContext, rng: np.random.Generator, max_tries: int):
        if max_tries < 1:
            raise ValueError("max_tries must be >= 1")
        self.ctx = ctx
        self.rng = rng
        self.max_tries = max_tries
        self.index = ctx.index
        self.n_rows = len(ctx.index)
        self.step = ctx.index[1] - ctx.index[0]
        self.pad = int(CONFLICT_PAD // self.step)
        self.months_arr = ctx.index.month.to_numpy()
        self.dist_stations = {str(s) for s in ctx.dist.index} & {str(s) for s in ctx.dist.columns}

        self._tables: dict[str, _VarTables] = {}
        self._time_ok_cache: dict[tuple[int, frozenset[int] | None], np.ndarray] = {}
        self._near_cache: dict[str, frozenset[str]] = {}
        self._nn_cache: dict[tuple, dict[str, list[str]]] = {}
        # variable -> [(start_row, end_row_exclusive, stations)] of everything placed so far
        self._claimed: dict[str, list[tuple[int, int, frozenset[str]]]] = {}

    # ---- data preparation ------------------------------------------------

    def _vt(self, variable: str, label: str, family: str) -> _VarTables:
        ctx = self.ctx
        if variable in self._tables:
            return self._tables[variable]
        if variable not in ctx.flags:
            raise PlacementError(f"{label}: ctx.flags has no table for {variable!r}")
        needs_values = variable == PRECIP or family == "stuck"
        if needs_values and variable not in ctx.values:
            raise PlacementError(
                f"{label}: ctx.values has no {variable!r} table, which {family} needs"
            )

        flags = ctx.flags[variable]
        columns = [str(c) for c in flags.columns]
        fl = flags.reindex(ctx.index).to_numpy()
        bad = ~(fl <= 0)  # flag > 0 or missing flag row (NaN)
        prev_finite = prec = None
        if variable in ctx.values:
            pre = ctx.index[0] - self.step
            ext = ctx.index.insert(0, pre)
            vals = ctx.values[variable].reindex(index=ext, columns=flags.columns).to_numpy()
            finite = np.isfinite(vals)
            bad = bad | ~finite[1:]
            prev_finite = np.ascontiguousarray(finite[:-1].T)
            if variable == PRECIP:
                prec = np.ascontiguousarray(vals[1:].T)
        vt = _VarTables(
            {s: j for j, s in enumerate(columns)},
            np.ascontiguousarray(bad.T),
            prev_finite,
            prec,
        )
        self._tables[variable] = vt
        return vt

    def _allowed_months(self, variable: str, label: str) -> frozenset[int] | None:
        months = {int(m) for m in self.ctx.months} if self.ctx.months is not None else None
        if variable == PRECIP:
            months = set(PRECIP_MONTHS) if months is None else months & PRECIP_MONTHS
        if months is not None and not months:
            raise PlacementError(f"{label}: no allowed month remains for {variable}")
        return None if months is None else frozenset(months)

    def _time_ok(self, n: int, allowed: frozenset[int] | None) -> np.ndarray:
        """Bool per start row: the whole n-step window fits the index and the allowed months."""
        key = (n, allowed)
        if key not in self._time_ok_cache:
            if allowed is None:
                ok = np.ones(self.n_rows - n + 1, dtype=bool)
            else:
                ok = _window_counts(~np.isin(self.months_arr, sorted(allowed)), n) == 0
            self._time_ok_cache[key] = ok
        return self._time_ok_cache[key]

    # ---- stations --------------------------------------------------------

    def _usable(self, pool: list[str], variable: str, vt: _VarTables) -> list[str]:
        excluded = EXCLUDED_ALL | EXCLUDED_BY_VARIABLE.get(variable, frozenset())
        seen: set[str] = set()
        out = []
        for s in map(str, pool):
            if s in excluded or s in seen or s not in vt.pos or s not in self.dist_stations:
                continue
            seen.add(s)
            out.append(s)
        return out

    def _nearest(self, key: tuple, stations: list[str], k: int) -> dict[str, list[str]]:
        if key not in self._nn_cache:
            sub = self.ctx.dist.loc[stations, stations]
            self._nn_cache[key] = nearest_neighbors(sub, k=k)
        return self._nn_cache[key]

    def _group(
        self, pool_name: str, variable: str, usable: list[str], target: str, n_group: int
    ) -> list[str]:
        """Stations the scenario writes to: the target, plus its n-1 nearest pool neighbours."""
        if n_group <= 1:
            return [target]
        nn = self._nearest(("pool", pool_name, variable, n_group - 1), usable, n_group - 1)
        return [target, *nn[target]]

    def _dry_group(self, variable: str, vt: _VarTables, target: str, label: str) -> list[str]:
        ctx = self.ctx
        if ctx.neighbors is not None and target in ctx.neighbors:
            nbrs = [str(s) for s in ctx.neighbors[target][:RAIN_INJECT_NEIGHBORS]]
        else:
            excluded = EXCLUDED_ALL | EXCLUDED_BY_VARIABLE.get(variable, frozenset())
            net = [
                str(s)
                for s in ctx.dist.index
                if str(s) not in excluded and str(s) in vt.pos
            ]
            nbrs = self._nearest(("network", variable), net, RAIN_INJECT_NEIGHBORS)[target]
        missing = [s for s in nbrs if s not in vt.pos]
        if missing:
            raise PlacementError(f"{label}: neighbour station(s) {missing} not in the {variable} tables")
        return [target, *nbrs]

    def _near(self, station: str) -> frozenset[str]:
        """Stations within CONFLICT_KM of ``station`` (itself included)."""
        if station not in self._near_cache:
            row = self.ctx.dist.loc[station]
            self._near_cache[station] = frozenset(
                str(s) for s in row.index[row.to_numpy() <= CONFLICT_KM]
            ) | {station}
        return self._near_cache[station]

    # ---- one spec --------------------------------------------------------

    def _data_ok(
        self,
        family: str,
        n: int,
        base_ok: np.ndarray,
        vt: _VarTables,
        group: list[str],
        dry_group: list[str] | None,
    ) -> np.ndarray:
        ok = base_ok.copy()
        max_bad = _max_bad(n, MAX_GAP_SHARE)
        for s in group:
            ok &= _window_counts(vt.bad[vt.pos[s]], n) <= max_bad
            if not ok.any():
                return ok
        target = vt.pos[group[0]]
        if family == "stuck":
            ok &= vt.prev_finite[target][: ok.size]
        if family in RAIN_MIN_MM:
            rain = np.nan_to_num(vt.prec[target], nan=0.0)
            ok &= _window_sums(rain, n) >= RAIN_MIN_MM[family] - _RAIN_TOL
        if dry_group is not None:
            for s in dry_group:
                if not ok.any():
                    break
                ok &= _window_counts(~(vt.prec[vt.pos[s]] == 0), n) == 0
        return ok

    def _drop_conflicts(
        self, ok: np.ndarray, variable: str, group: list[str], n: int
    ) -> np.ndarray:
        zone = frozenset().union(*(self._near(s) for s in group))
        for a, b, stations in self._claimed.get(variable, ()):
            if zone.isdisjoint(stations):
                continue
            # window [s, s+n) is < pad away from (or overlaps) [a, b)  <=>  a-n-pad < s < b+pad
            lo, hi = max(0, a - n - self.pad + 1), min(ok.size, b + self.pad)
            if lo < hi:
                ok[lo:hi] = False
        return ok

    def place_one(self, index: int, spec: dict) -> Scenario:
        ctx = self.ctx
        try:
            family = spec["family"]
            variable = spec["variable"]
            params = {str(k): float(v) for k, v in spec["params"].items()}
            n = int(spec["duration_steps"])
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise PlacementError(f"spec #{index} is malformed ({err!r}): {spec!r}") from err
        label = (
            f"spec #{index} (family={family!r}, variable={variable!r}, params={params}, "
            f"duration_steps={n})"
        )
        if family not in REQUIRED_PARAMS:
            raise PlacementError(f"{label}: unknown family")
        if (family in RAIN_FAMILIES) != (variable == PRECIP):
            raise PlacementError(f"{label}: family and variable do not match")
        if not 1 <= n <= self.n_rows:
            raise PlacementError(f"{label}: duration_steps must be in 1..{self.n_rows}")

        split = spec.get("split", ctx.split)
        pool_name = spec.get("pool") or (ctx.pool if "split" not in spec else None) or split
        if pool_name not in ctx.pools:
            raise PlacementError(
                f"{label}: no station pool {pool_name!r} in the context (have {sorted(ctx.pools)})"
            )

        vt = self._vt(variable, label, family)
        usable = self._usable(ctx.pools[pool_name], variable, vt)
        n_group = int(params["n"]) if family == "multi_offset" else 1
        if len(usable) < max(n_group, 1):
            raise PlacementError(
                f"{label}: pool {pool_name!r} has {len(usable)} usable station(s), "
                f"{n_group} needed"
            )
        base_ok = self._time_ok(n, self._allowed_months(variable, label))
        if not base_ok.any():
            raise PlacementError(f"{label}: no {n}-step window fits the allowed months/index")

        data_blocked = conflict_blocked = 0
        order = self.rng.permutation(len(usable))[: self.max_tries]
        for pos in order:
            target = usable[int(pos)]
            group = self._group(pool_name, variable, usable, target, n_group)
            dry = (
                self._dry_group(variable, vt, target, label)
                if family == "rain_inject"
                else None
            )
            ok = self._data_ok(family, n, base_ok, vt, group, dry)
            if not ok.any():
                data_blocked += 1
                continue
            ok = self._drop_conflicts(ok, variable, group, n)
            if not ok.any():
                conflict_blocked += 1
                continue

            candidates = np.flatnonzero(ok)
            row = int(candidates[self.rng.integers(candidates.size)])
            start = ctx.index[0] + row * self.step
            end = start + n * self.step
            try:
                scenario = Scenario(
                    id=f"{split}-{family}-{variable[:2]}-{index:04d}",
                    family=family,
                    variable=variable,
                    stations=tuple(group),
                    start=start.to_pydatetime(),
                    end=end.to_pydatetime(),
                    params=params,
                    split=split,
                )
            except ValidationError as err:
                raise PlacementError(f"{label}: invalid scenario: {err}") from err
            self._claimed.setdefault(variable, []).append((row, row + n, frozenset(group)))
            return scenario

        raise PlacementError(
            f"{label}: no eligible, conflict-free window in pool {pool_name!r}; examined "
            f"{len(order)} of {len(usable)} stations ({data_blocked} without any window passing "
            f"the gap/season/rain rules, {conflict_blocked} fully blocked by the 3 km / 24 h "
            "conflict rule)"
        )


def place(
    specs: list[dict],
    ctx: PlacementContext,
    rng: np.random.Generator,
    max_tries: int = 200,
) -> list[Scenario]:
    """Place every spec; scenarios come back in spec order (the order sets ``id`` and injector RNG).

    For each spec the pool's stations are visited in a random order (at most ``max_tries`` of
    them).  At each one all start rows satisfying the data rules and not in conflict with
    earlier placements are computed exactly, and one is drawn uniformly; the first station with
    any such row wins.  Raises ``PlacementError`` naming the spec if none does.
    """
    placer = _Placer(ctx, rng, max_tries)
    return [placer.place_one(i, spec) for i, spec in enumerate(specs)]
