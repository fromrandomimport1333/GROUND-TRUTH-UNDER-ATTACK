"""Placement: split, eligibility, conflicts and the per-family rules of spec section 6.

Every test builds a toy ``PlacementContext`` on the real 2024 5-minute index (105 408 rows)
with a handful of synthetic stations on a line, so nothing depends on ``data/clean``.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from attacks.placement import (
    PlacementContext,
    PlacementError,
    eligible,
    place,
    split_stations,
)
from attacks.schema import Scenario, Suite
from data_prep.stations import nearest_neighbors

STEP = pd.Timedelta(minutes=5)
INDEX = pd.date_range("2024-01-01", "2024-12-31 23:55", freq="5min", tz="UTC", name="time")
N = len(INDEX)
HOUR = 12  # steps per hour
DAY = 24 * HOUR
T, RH, P = "air_temperature", "relative_humidity", "precipitation"
VARIABLES = (T, RH, P)
CONFLICT_KM = 3.0
CONFLICT_PAD = pd.Timedelta(hours=24)


# --------------------------------------------------------------------------- helpers


def line_layout(xs: dict[str, float]) -> pd.DataFrame:
    """Station x station distances (km) for stations on a line at positions ``xs``."""
    ids = list(xs)
    x = np.array([xs[s] for s in ids], dtype=float)
    return pd.DataFrame(np.abs(x[:, None] - x[None, :]), index=ids, columns=ids)


def make_ctx(
    xs: dict[str, float],
    *,
    split: str = "dev",
    pools: dict[str, list[str]] | None = None,
    flags: dict[str, pd.DataFrame] | None = None,
    values: dict[str, pd.DataFrame] | None = None,
    months: tuple[int, ...] | None = None,
) -> PlacementContext:
    stations = list(xs)
    fl = {
        v: pd.DataFrame(np.zeros((N, len(stations)), np.int8), index=INDEX, columns=stations)
        for v in VARIABLES
    }
    fl.update(flags or {})
    rng = np.random.default_rng(1)
    base = {
        T: 10 + 5 * np.sin(2 * np.pi * np.arange(N) / DAY)[:, None],
        RH: 60 + 20 * np.cos(2 * np.pi * np.arange(N) / DAY)[:, None],
        P: np.zeros((N, 1)),
    }
    shape = (N, len(stations))
    vals = {
        v: pd.DataFrame(
            (base[v] + (rng.normal(0, 0.1, shape) if v != P else np.zeros(shape))).astype(
                np.float32
            ),
            index=INDEX,
            columns=stations,
        )
        for v in VARIABLES
    }
    vals.update(values or {})
    return PlacementContext(
        flags=fl,
        values=vals,
        dist=line_layout(xs),
        pools=pools or {"dev": stations, "heldout": stations},
        index=INDEX,
        split=split,
        months=months,
    )


def rainy(stations: list[str], *, p_wet_hour: float, mm: float = 0.5, seed: int = 3) -> pd.DataFrame:
    """Hourly wet/dry blocks per station: ``mm`` per 5-min step during a wet hour."""
    rng = np.random.default_rng(seed)
    wet_hours = rng.random((N // HOUR + 1, len(stations))) < p_wet_hour
    wet = np.repeat(wet_hours, HOUR, axis=0)[:N]
    return pd.DataFrame((wet * mm).astype(np.float32), index=INDEX, columns=stations)


def spec(family: str, variable: str, hours: float, **params: float) -> dict:
    steps = int(round(hours * HOUR))
    params = {k: float(v) for k, v in params.items()}
    if family == "flatline":
        params["w_steps"] = float(steps)
    if family == "rain_inject":
        params["duration_steps"] = float(steps)
    return {"family": family, "variable": variable, "params": params, "duration_steps": steps}


def start_idx(sc: Scenario) -> int:
    return int((pd.Timestamp(sc.start) - INDEX[0]) / STEP)


def end_idx(sc: Scenario) -> int:
    """Exclusive end row."""
    return int((pd.Timestamp(sc.end) - INDEX[0]) / STEP)


def in_months(sc: Scenario, months: range | tuple[int, ...]) -> bool:
    first, last = pd.Timestamp(sc.start), pd.Timestamp(sc.end) - STEP
    window = pd.date_range(first, last, freq="D").append(pd.DatetimeIndex([last]))
    return all(m in months for m in window.month)


FAR = {"1": 0.0, "2": 20.0, "3": 40.0, "4": 60.0, "5": 80.0, "6": 100.0}  # nothing conflicts


# --------------------------------------------------------------------------- split


def test_split_is_deterministic_and_disjoint():
    stations = [str(i) for i in range(1, 155) if i != 18]
    a = split_stations(stations)
    b = split_stations(list(reversed(stations)))  # input order must not matter
    assert a == b
    assert set(a) == {"dev", "heldout"}
    assert not set(a["dev"]) & set(a["heldout"])
    assert set(a["dev"]) | set(a["heldout"]) == set(stations)
    assert len(a["dev"]) == round(0.7 * len(stations))
    assert a["dev"] == sorted(a["dev"], key=int)  # stable presentation order
    other = split_stations(stations, seed=7)
    assert other["dev"] != a["dev"]  # the seed actually matters
    assert len(split_stations(stations, dev_share=0.5)["dev"]) == round(0.5 * len(stations))


# --------------------------------------------------------------------------- eligibility


def test_eligible_counts_gap_fill_share():
    flags = pd.Series(np.zeros(200, np.int8))
    flags.iloc[10] = 2  # 1 filled point in a 20-point window = 5 % -> still eligible
    assert eligible(flags, 0, 20)
    flags.iloc[11] = 1  # 2 of 20 = 10 % -> rejected
    assert not eligible(flags, 0, 20)
    assert eligible(flags, 20, 20)  # a clean window elsewhere is fine
    assert eligible(flags, 0, 20, max_gap_share=0.10)
    assert not eligible(flags, 190, 20)  # runs off the end
    assert not eligible(flags, -1, 20)


def test_window_rejected_when_gap_filled():
    # every 10th point is gap-filled everywhere (10 % > 5 %), except for one clean region
    pattern = np.zeros((N, 2), np.int8)
    pattern[::10] = 2
    r0, r1 = 20_000, 20_000 + 10 * DAY
    pattern[r0:r1] = 0
    flags = {T: pd.DataFrame(pattern, index=INDEX, columns=["1", "2"])}
    ctx = make_ctx({"1": 0.0, "2": 20.0}, flags=flags)
    specs = [spec("offset", T, 6, k=2.0), spec("noise", T, 6, k=1.0)]

    scenarios = place(specs, ctx, np.random.default_rng(0))

    for sc in scenarios:
        a, b = start_idx(sc), end_idx(sc)
        col = flags[T][sc.stations[0]]
        assert eligible(col, a, b - a)
        assert (col.to_numpy()[a:b] > 0).sum() <= 0.05 * (b - a)
        # the only eligible area is the clean region (plus the 5 % allowance at its edges)
        assert r0 - (b - a) // 2 <= a and b <= r1 + (b - a) // 2

    all_filled = {T: pd.DataFrame(np.full((N, 2), 2, np.int8), index=INDEX, columns=["1", "2"])}
    with pytest.raises(PlacementError, match="offset"):
        place(specs[:1], make_ctx({"1": 0.0, "2": 20.0}, flags=all_filled), np.random.default_rng(0))


# --------------------------------------------------------------------------- precipitation season


def test_precip_never_placed_in_winter():
    # rain everywhere all year, no gaps: only the month rule can keep scenarios out of winter
    wet = pd.DataFrame(np.full((N, len(FAR)), 0.5, np.float32), index=INDEX, columns=list(FAR))
    ctx = make_ctx(FAR, values={P: wet})
    specs = (
        [spec("rain_scale", P, h, f=2.0) for h in (6, 24) for _ in range(6)]
        + [spec("rain_suppress", P, h) for h in (6, 24) for _ in range(6)]
    )
    scenarios = place(specs, ctx, np.random.default_rng(5))
    assert len(scenarios) == len(specs)
    for sc in scenarios:
        assert in_months(sc, range(4, 11)), (sc.id, sc.start, sc.end)
        assert pd.Timestamp(sc.end) <= pd.Timestamp("2024-11-01", tz="UTC")

    # rain only in January: nothing may be placed even though the data would qualify
    jan = np.zeros((N, len(FAR)), np.float32)
    jan[: 31 * DAY] = 0.5
    ctx_jan = make_ctx(FAR, values={P: pd.DataFrame(jan, index=INDEX, columns=list(FAR))})
    with pytest.raises(PlacementError, match="rain_scale"):
        place([spec("rain_scale", P, 24, f=2.0)], ctx_jan, np.random.default_rng(5))


def test_months_restriction_applies_to_every_variable():
    ctx = make_ctx(FAR, months=(6,))
    specs = [spec("offset", T, 72, k=1.0) for _ in range(10)]
    for sc in place(specs, ctx, np.random.default_rng(2)):
        assert in_months(sc, (6,))


# --------------------------------------------------------------------------- conflicts


def _near(dist: pd.DataFrame, a: Scenario, b: Scenario) -> bool:
    return bool((dist.loc[list(a.stations), list(b.stations)].to_numpy() <= CONFLICT_KM).any())


def _times_conflict(a: Scenario, b: Scenario) -> bool:
    """Windows overlap once either is padded by 24 h on both sides (end exclusive)."""
    return (
        pd.Timestamp(a.start) - CONFLICT_PAD < pd.Timestamp(b.end)
        and pd.Timestamp(b.start) - CONFLICT_PAD < pd.Timestamp(a.end)
    )


def test_conflicting_scenarios_not_overlapping():
    # A: three stations within 3 km of each other (1-3 are chained), B: two stations, C: alone
    xs = {"1": 0.0, "2": 1.5, "3": 2.5, "4": 30.0, "5": 31.0, "6": 60.0}
    ctx = make_ctx(xs)
    variables = (T, RH)
    families = ("offset", "noise", "drift")
    hours = (24, 72, 168)
    specs = []
    for i in range(50):
        fam = families[i % 3]
        params = {"k": 2.0}
        specs.append(
            {
                "family": fam,
                "variable": variables[i % 2],
                "params": params,
                "duration_steps": hours[(i // 2) % 3] * HOUR,
            }
        )

    scenarios = place(specs, ctx, np.random.default_rng(11))
    assert len(scenarios) == 50

    near_pairs = 0
    for i, a in enumerate(scenarios):
        for b in scenarios[i + 1 :]:
            if a.variable != b.variable or not _near(ctx.dist, a, b):
                continue
            near_pairs += 1
            assert not _times_conflict(a, b), (a.id, b.id)
    assert near_pairs >= 10  # the rule was actually exercised, not vacuously true


def test_conflict_zone_blocks_second_scenario():
    # the only gap-free region is 30 h wide: two 12 h windows can never be 24 h apart in it
    region = (10_000, 10_000 + 30 * HOUR)

    def flags_for(cols):
        f = np.full((N, len(cols)), 2, np.int8)
        f[region[0] : region[1]] = 0
        return {T: pd.DataFrame(f, index=INDEX, columns=cols)}

    twelve = [spec("offset", T, 12, k=1.0), spec("offset", T, 12, k=2.0)]

    close = {"1": 0.0, "2": 1.0}  # 1 km apart -> conflicting stations
    ctx = make_ctx(close, flags=flags_for(list(close)))
    assert len(place(twelve[:1], ctx, np.random.default_rng(0))) == 1
    with pytest.raises(PlacementError, match=r"spec #1"):
        place(twelve, ctx, np.random.default_rng(0))

    # 20 km apart the two stations do not interact: the same time slot may be reused
    apart = {"1": 0.0, "2": 20.0}
    ctx = make_ctx(apart, flags=flags_for(list(apart)))
    placed = place(twelve, ctx, np.random.default_rng(0))
    assert {sc.stations[0] for sc in placed} == {"1", "2"}  # forced onto different stations


def test_conflicts_are_per_variable():
    # same station, same single usable region, different variables: both fit
    region = (10_000, 10_000 + 30 * HOUR)
    f = np.full((N, 1), 2, np.int8)
    f[region[0] : region[1]] = 0
    flags = {v: pd.DataFrame(f, index=INDEX, columns=["1"]) for v in (T, RH)}
    ctx = make_ctx({"1": 0.0}, flags=flags)
    placed = place(
        [spec("offset", T, 12, k=1.0), spec("offset", RH, 12, k=1.0)],
        ctx,
        np.random.default_rng(0),
    )
    assert {sc.variable for sc in placed} == {T, RH}


# --------------------------------------------------------------------------- rain families


def test_rain_inject_window_is_dry():
    xs = {str(i): 0.9 * i + 0.05 * i * i for i in range(1, 8)}  # convex: no distance ties
    xs["8"] = 60.0  # far away and always wet: never among anyone's 5 nearest neighbours
    stations = list(xs)
    precip = rainy(stations, p_wet_hour=0.10)
    precip["8"] = 0.3
    ctx = make_ctx(xs, values={P: precip})
    nn = nearest_neighbors(ctx.dist, k=5)
    assert all(ctx.dist[s].drop(s).is_unique for s in stations)

    specs = [spec("rain_inject", P, h, total_mm=m) for h in (1, 3) for m in (2, 5, 10, 20) for _ in range(3)]
    scenarios = place(specs, ctx, np.random.default_rng(4))

    assert len(scenarios) == len(specs)
    for sc in scenarios:
        assert len(sc.stations) == 1
        target = sc.stations[0]
        group = [target, *nn[target]]
        window = precip.iloc[start_idx(sc) : end_idx(sc)][group].to_numpy()
        assert (window == 0).all(), (sc.id, group)
        assert in_months(sc, range(4, 11))
        assert end_idx(sc) - start_idx(sc) == int(sc.params["duration_steps"])


def test_rain_scale_and_suppress_windows_contain_rain():
    stations = list(FAR)
    precip = rainy(stations, p_wet_hour=0.12)
    ctx = make_ctx(FAR, values={P: precip})
    specs = [spec("rain_scale", P, 6, f=2.0) for _ in range(8)] + [
        spec("rain_suppress", P, 6) for _ in range(8)
    ]
    scenarios = place(specs, ctx, np.random.default_rng(9))
    for sc in scenarios:
        total = precip[sc.stations[0]].iloc[start_idx(sc) : end_idx(sc)].sum()
        need = 2.0 if sc.family == "rain_scale" else 5.0
        assert total >= need - 1e-4, (sc.id, total)


# --------------------------------------------------------------------------- pools


def test_heldout_specs_use_heldout_stations_only():
    xs = {str(i): 10.0 * i for i in range(1, 31)}  # 10 km apart: only same-station conflicts
    stations = list(xs)
    pools = split_stations(stations, seed=2026)
    pools = {**pools, "all": stations}
    precip = rainy(stations, p_wet_hour=0.15)
    ctx = make_ctx(xs, split="heldout", pools=pools, values={P: precip})

    heldout_specs = [
        spec("offset", T, 12, k=0.75),
        spec("noise", RH, 12, k=1.5),
        spec("gain", T, 24, g=1.5),
        spec("intermittent_offset", RH, 48, k=1.5),
        spec("stuck", T, 12),
        spec("rain_scale", P, 12, f=2.5),
        spec("rain_suppress", P, 12),
        spec("rain_inject", P, 2, total_mm=7.0),
    ] * 3
    scenarios = place(heldout_specs, ctx, np.random.default_rng(6))
    assert len(scenarios) == len(heldout_specs)
    for sc in scenarios:
        assert sc.split == "heldout"
        assert sc.id.startswith("heldout-")
        assert set(sc.stations) <= set(pools["heldout"]), sc.id
        assert not set(sc.stations) & set(pools["dev"]), sc.id

    # a per-spec split overrides the context default, and picks that pool
    mixed = [dict(s, split="dev") for s in heldout_specs[:8]] + heldout_specs[:8]
    out = place(mixed, ctx, np.random.default_rng(6))
    for sc, s in zip(out, mixed):
        want = s.get("split", "heldout")
        assert sc.split == want and sc.id.startswith(f"{want}-")
        assert set(sc.stations) <= set(pools[want])


def test_multi_offset_lists_target_and_nearest_pool_neighbours():
    # irregular spacing so numeric order != distance order; stations not in the pool sit
    # between pool members and must never be used
    squares = [0, 1, 4, 9, 16, 25, 36, 49, 64, 81, 100, 121]
    xs = {str(i): 0.7 * x for i, x in enumerate(squares, start=1)}
    pool = ["1", "3", "4", "6", "7", "9", "10", "12"]
    ctx = make_ctx(xs, pools={"dev": pool, "heldout": pool})
    for n in (2, 3, 5, 8):
        specs = [spec("multi_offset", T, 24, k=2.0, n=n) for _ in range(3)]
        for sc in place(specs, ctx, np.random.default_rng(n)):
            target, rest = sc.stations[0], list(sc.stations[1:])
            assert len(sc.stations) == n and len(set(sc.stations)) == n
            assert set(sc.stations) <= set(pool)
            row = ctx.dist.loc[target, [s for s in pool if s != target]]
            assert row.is_unique  # no distance ties, so the expectation is unambiguous
            assert rest == row.sort_values().index[: n - 1].tolist()

    with pytest.raises(PlacementError, match="multi_offset"):
        tiny = make_ctx(xs, pools={"dev": ["1", "3", "4"], "heldout": ["1", "3", "4"]})
        place([spec("multi_offset", T, 24, k=2.0, n=5)], tiny, np.random.default_rng(0))


def test_multi_offset_requires_every_station_to_be_eligible():
    # station "2" (the only neighbour of "1" in this pool) is gap-filled everywhere
    f = np.zeros((N, 2), np.int8)
    f[:, 1] = 2
    ctx = make_ctx(
        {"1": 0.0, "2": 1.0},
        flags={T: pd.DataFrame(f, index=INDEX, columns=["1", "2"])},
    )
    with pytest.raises(PlacementError, match="multi_offset"):
        place([spec("multi_offset", T, 24, k=2.0, n=2)], ctx, np.random.default_rng(0))


# --------------------------------------------------------------------------- stuck


@pytest.mark.parametrize("block_start", [0, 1000])
def test_stuck_requires_finite_value_before_window(block_start):
    # finite data only in a 14-row block: windows of 12 rows fit at block_start + 0..2, but a
    # window starting at block_start has a non-finite (or missing) row just before it
    values = np.full((N, 1), np.nan, np.float32)
    values[block_start : block_start + 14] = 5.0
    ctx = make_ctx(
        {"1": 0.0},
        values={T: pd.DataFrame(values, index=INDEX, columns=["1"])},
    )
    starts = set()
    for seed in range(40):
        (sc,) = place([spec("stuck", T, 1)], ctx, np.random.default_rng(seed))
        starts.add(start_idx(sc))
    assert starts <= {block_start + 1, block_start + 2}
    assert starts == {block_start + 1, block_start + 2}  # both are reachable


def test_non_stuck_families_do_not_need_a_previous_value():
    values = np.full((N, 1), np.nan, np.float32)
    values[0:14] = 5.0
    ctx = make_ctx({"1": 0.0}, values={T: pd.DataFrame(values, index=INDEX, columns=["1"])})
    starts = {
        start_idx(place([spec("offset", T, 1, k=1.0)], ctx, np.random.default_rng(s))[0])
        for s in range(40)
    }
    assert starts == {0, 1, 2}


# --------------------------------------------------------------------------- bookkeeping


def test_ids_grid_alignment_and_params():
    ctx = make_ctx(FAR, values={P: rainy(list(FAR), p_wet_hour=0.1)})
    specs = [
        spec("offset", T, 6, k=2.0),
        spec("spike", RH, 24, k=4.0, n=3),
        spec("flatline", T, 3),
        spec("rain_suppress", P, 6),
        spec("drift", RH, 24, k=3.0),
        spec("multi_offset", T, 24, k=2.0, n=3),
    ]
    scenarios = place(specs, ctx, np.random.default_rng(1))
    assert len(scenarios) == len(specs)
    for i, (sc, s) in enumerate(zip(scenarios, specs)):
        assert isinstance(sc, Scenario)
        assert sc.id == f"dev-{s['family']}-{s['variable'][:2]}-{i:04d}"
        assert sc.family == s["family"] and sc.variable == s["variable"] and sc.split == "dev"
        assert sc.params == s["params"]
        a, b = pd.Timestamp(sc.start), pd.Timestamp(sc.end)
        assert a.tzinfo is not None and b.tzinfo is not None
        assert (a - INDEX[0]) % STEP == pd.Timedelta(0)
        assert a >= INDEX[0] and b <= INDEX[-1] + STEP  # fully inside 2024, end exclusive
        assert (b - a) / STEP == s["duration_steps"]


def test_place_is_deterministic_given_the_rng():
    ctx = make_ctx(FAR, values={P: rainy(list(FAR), p_wet_hour=0.1)})
    specs = [spec("offset", T, 24, k=1.0), spec("rain_scale", P, 6, f=2.0), spec("stuck", RH, 6)] * 4
    first = place(specs, ctx, np.random.default_rng(42))
    again = place(specs, ctx, np.random.default_rng(42))
    other = place(specs, ctx, np.random.default_rng(43))
    assert first == again
    assert first != other


def test_placement_error_names_the_spec():
    # rain nowhere -> the third spec (index 2) cannot contain 2 mm
    ctx = make_ctx(FAR)
    specs = [spec("offset", T, 6, k=1.0), spec("stuck", RH, 6), spec("rain_scale", P, 6, f=2.0)]
    with pytest.raises(PlacementError) as err:
        place(specs, ctx, np.random.default_rng(0))
    msg = str(err.value)
    assert "spec #2" in msg and "rain_scale" in msg and "precipitation" in msg


def test_unknown_pool_or_missing_values_is_a_placement_error():
    ctx = make_ctx(FAR, split="nonexistent")
    with pytest.raises(PlacementError, match="pool"):
        place([spec("offset", T, 6, k=1.0)], ctx, np.random.default_rng(0))

    ctx = make_ctx(FAR)
    del ctx.values[P]
    with pytest.raises(PlacementError, match="precipitation"):
        place([spec("rain_scale", P, 6, f=2.0)], ctx, np.random.default_rng(0))


def test_station_156_never_used_for_precipitation_and_18_never_at_all():
    xs = {"18": 0.0, "156": 5.0, "77": 10.0, "78": 40.0}
    wet = pd.DataFrame(np.full((N, 4), 0.5, np.float32), index=INDEX, columns=list(xs))
    ctx = make_ctx(xs, values={P: wet})
    specs = [spec("rain_scale", P, 6, f=2.0)] * 6 + [spec("offset", T, 6, k=1.0)] * 6
    for sc in place(specs, ctx, np.random.default_rng(0)):
        assert "18" not in sc.stations
        if sc.variable == P:
            assert "156" not in sc.stations


# --------------------------------------------------------------------------- integration


def test_placed_suite_injects_cleanly():
    """Placement output must satisfy the injector's contract (grid, overlap, stuck prev)."""
    from attacks.injector import inject

    xs = {str(i): float(i) for i in range(1, 7)} | {"7": 40.0, "8": 45.0, "9": 50.0}
    stations = list(xs)
    precip = rainy(stations, p_wet_hour=0.12)
    ctx = make_ctx(xs, values={P: precip})
    specs = []
    for _ in range(3):
        specs += [
            spec("offset", T, 6, k=2.0),
            spec("drift", RH, 24, k=3.0),
            spec("spike", T, 24, k=4.0, n=3),
            spec("noise", RH, 6, k=1.0),
            spec("flatline", T, 3),
            spec("stuck", RH, 6),
            spec("multi_offset", T, 24, k=2.0, n=3),
            spec("gain", T, 24, g=1.5),
            spec("intermittent_offset", RH, 48, k=1.5),
            spec("rain_scale", P, 6, f=2.0),
            spec("rain_suppress", P, 6),
            spec("rain_inject", P, 3, total_mm=5.0),
        ]
    scenarios = place(specs, ctx, np.random.default_rng(21))
    suite = Suite(name="toy", seed=1, scenarios=tuple(scenarios))

    sigma = pd.DataFrame(
        {T: [0.5] * len(stations), RH: [2.0] * len(stations)},
        index=pd.Index(stations, name="station"),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)  # a skipped stuck station warns
        _, labels = inject(suite, ctx.values, sigma)

    assert len(labels) == sum(len(sc.stations) for sc in scenarios)
    assert set(labels["scenario_id"]) == {sc.id for sc in scenarios}
