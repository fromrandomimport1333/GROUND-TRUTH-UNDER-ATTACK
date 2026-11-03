"""Injector + labels: windows land where the labels say, and nothing else changes.

All tests use a small synthetic clean set (6 stations, 4 days straddling the 2023/2024
boundary) so they never depend on ``data/clean``.
"""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from attacks.families import apply_fault, clip_physical
from attacks.injector import LABEL_COLUMNS, inject
from attacks.schema import Scenario, Suite

STEP = timedelta(minutes=5)
STATIONS = ["1", "2", "3", "4", "5", "6"]
IDX = pd.date_range("2023-12-31", "2024-01-04", freq="5min", tz="UTC", inclusive="left")
T0 = datetime(2024, 1, 2, 6, 0, tzinfo=timezone.utc)
FAULT_FLOOR = pd.Timestamp("2024-01-01", tz="UTC")

DEFAULT_PARAMS = {
    "offset": {"k": 2.0},
    "drift": {"k": 3.0},
    "spike": {"k": 4.0, "n": 3.0},
    "noise": {"k": 1.0},
    "flatline": {"w_steps": 6.0},
    "stuck": {},
    "multi_offset": {"k": 2.0, "n": 3.0},
    "gain": {"g": 1.5},
    "intermittent_offset": {"k": 2.0},
    "rain_scale": {"f": 2.0},
    "rain_suppress": {},
    "rain_inject": {"total_mm": 5.0, "duration_steps": 12.0},
}

SIGMA = pd.DataFrame(
    {
        "air_temperature": [0.5, 0.6, 0.7, 0.4, 0.8, 0.55],
        "relative_humidity": [2.0, 2.5, 3.0, 1.6, 3.8, 2.2],
    },
    index=pd.Index(STATIONS, name="station"),
)

# station -> neighbours in rank order (deliberately not numerically ordered)
NEIGHBORS = pd.DataFrame(
    [
        (s, rank, nb, 1.0 + rank)
        for s, nbs in {
            "1": ["4", "2", "6"],
            "2": ["1", "3", "5"],
            "3": ["2", "6", "4"],
        }.items()
        for rank, nb in enumerate(nbs, start=1)
    ],
    columns=["station", "rank", "neighbor", "distance_km"],
)


def make_clean() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(0)
    n, m = len(IDX), len(STATIONS)
    t = np.arange(n)[:, None]
    wet = rng.random((n, m)) < 0.3
    raw = {
        "air_temperature": 10 + 5 * np.sin(2 * np.pi * t / 288) + rng.normal(0, 0.3, (n, m)),
        "relative_humidity": 60 + 20 * np.cos(2 * np.pi * t / 288) + rng.normal(0, 1, (n, m)),
        "precipitation": np.where(wet, rng.exponential(0.2, (n, m)), 0.0),
    }
    return {
        v: pd.DataFrame(a.astype(np.float32), index=IDX, columns=STATIONS)
        for v, a in raw.items()
    }


def scen(
    i: int,
    family: str = "offset",
    variable: str = "air_temperature",
    stations: tuple[str, ...] = ("1",),
    start: datetime = T0,
    hours: float = 6,
    params: dict | None = None,
) -> Scenario:
    return Scenario(
        id=f"dev-{family}-{variable[:2]}-{i:04d}",
        family=family,
        variable=variable,
        stations=stations,
        start=start,
        end=start + timedelta(hours=hours),
        params=DEFAULT_PARAMS[family] if params is None else params,
        split="dev",
    )


def suite_of(*scenarios: Scenario, seed: int = 7) -> Suite:
    return Suite(name="unit", seed=seed, scenarios=tuple(scenarios))


def window_mask(labels: pd.DataFrame, table: pd.DataFrame, variable: str) -> np.ndarray:
    """Boolean (time, station) mask of every labelled cell of one variable."""
    mask = np.zeros(table.shape, dtype=bool)
    for row in labels[labels["variable"] == variable].itertuples():
        i0 = table.index.get_loc(row.start)
        i1 = table.index.searchsorted(row.end, side="left")  # end is exclusive
        mask[i0:i1, table.columns.get_loc(row.station)] = True
    return mask


def test_values_outside_windows_untouched():
    clean = make_clean()
    before = {v: df.copy() for v, df in clean.items()}
    suite = suite_of(
        scen(0, "offset", "air_temperature", ("1",)),
        scen(1, "noise", "relative_humidity", ("2",), start=T0 + timedelta(days=1), hours=3),
        scen(2, "rain_scale", "precipitation", ("3",), start=T0 + timedelta(hours=2), hours=4),
        scen(3, "multi_offset", "air_temperature", ("4",), start=T0 + timedelta(days=1),
             params={"k": 2.0, "n": 1.0}),
    )

    faulted, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    for v in clean:
        # inputs were not mutated
        pd.testing.assert_frame_equal(clean[v], before[v])
        out, ref = faulted[v].to_numpy(), clean[v].to_numpy()
        mask = window_mask(labels, clean[v], v)
        assert mask.any()
        assert np.array_equal(out[~mask], ref[~mask], equal_nan=True)  # exact equality
        assert (faulted[v].dtypes == clean[v].dtypes).all()
        assert faulted[v].index.equals(clean[v].index)
        assert faulted[v].columns.equals(clean[v].columns)
        assert (out[mask] != ref[mask]).any()  # and something did change inside


def test_one_label_row_per_station():
    clean = make_clean()
    suite = suite_of(
        scen(0, "multi_offset", stations=("1",), params={"k": 2.0, "n": 3.0}, hours=24),
    )

    faulted, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    # target "1" plus its 2 nearest neighbours by rank (4, 2), in that order
    assert labels["station"].tolist() == ["1", "4", "2"]
    assert (labels["scenario_id"] == suite.scenarios[0].id).all()
    assert (labels["n_stations"] == 3).all()
    assert (labels["family"] == "multi_offset").all()
    # every affected station really carries its own k * sigma_ref offset
    start, end = labels["start"].iloc[0], labels["end"].iloc[0]
    for row in labels.itertuples():
        win = slice(clean["air_temperature"].index.get_loc(start),
                    clean["air_temperature"].index.searchsorted(end))
        delta = (faulted["air_temperature"][row.station].to_numpy()[win].astype(np.float64)
                 - clean["air_temperature"][row.station].to_numpy()[win])
        expected = 2.0 * SIGMA.loc[row.station, "air_temperature"]
        np.testing.assert_allclose(delta, expected, atol=1e-4)
        assert row.size_phys == pytest.approx(expected)
    # a station outside the group is untouched
    assert np.array_equal(
        faulted["air_temperature"]["3"].to_numpy(), clean["air_temperature"]["3"].to_numpy()
    )


def test_multi_offset_with_stations_already_listed_is_not_fanned_out_again():
    clean = make_clean()
    suite = suite_of(
        scen(0, "multi_offset", stations=("2", "3", "5"), params={"k": 2.0, "n": 3.0}, hours=24),
    )
    _, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)
    assert labels["station"].tolist() == ["2", "3", "5"]
    assert (labels["n_stations"] == 3).all()


def test_multi_offset_without_enough_neighbours_is_an_error():
    clean = make_clean()
    suite = suite_of(
        scen(0, "multi_offset", stations=("1",), params={"k": 2.0, "n": 8.0}, hours=24),
    )
    with pytest.raises(ValueError, match="neighbou?r"):
        inject(suite, clean, SIGMA, neighbors=NEIGHBORS)


def test_labels_are_utc():
    clean = make_clean()
    suite = suite_of(
        scen(0, "drift", params={"k": 3.0}, hours=24),
        scen(1, "offset", "relative_humidity", ("2",)),
    )
    _, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    for col in ("start", "end", "onset"):
        dtype = labels[col].dtype
        assert isinstance(dtype, pd.DatetimeTZDtype), f"{col} is not tz-aware"
        assert str(dtype.tz) == "UTC"
    assert list(labels.columns) == LABEL_COLUMNS
    assert LABEL_COLUMNS == [
        "scenario_id", "suite", "family", "category", "variable", "station", "start",
        "end", "onset", "size", "size_phys", "unit", "duration_h", "n_stations", "split",
    ]
    assert (labels["category"] == "A").all()
    assert (labels["suite"] == "unit").all()
    assert labels["start"].iloc[0] == pd.Timestamp(T0)
    assert labels["duration_h"].tolist() == [24.0, 6.0]
    assert labels["unit"].tolist() == ["degC", "%"]
    assert labels["split"].tolist() == ["dev", "dev"]


def test_naive_clean_index_is_rejected():
    clean = make_clean()
    clean["air_temperature"] = clean["air_temperature"].tz_localize(None)
    with pytest.raises(ValueError, match="UTC"):
        inject(suite_of(scen(0)), clean, SIGMA, neighbors=NEIGHBORS)


def test_end_exclusive():
    clean = make_clean()
    sc = scen(0, "offset", hours=6)  # +2 * 0.5 = +1.0 degC everywhere in the window
    faulted, labels = inject(suite_of(sc), clean, SIGMA, neighbors=NEIGHBORS)

    changed = faulted["air_temperature"]["1"] != clean["air_temperature"]["1"]
    touched = changed.index[changed.to_numpy()]
    assert touched[0] == pd.Timestamp(sc.start)
    assert touched[-1] == pd.Timestamp(sc.end) - pd.Timedelta(STEP)  # last faulted = end - 5 min
    assert len(touched) == 6 * 12
    assert labels["end"].iloc[0] == pd.Timestamp(sc.end)
    # the step at `end` itself is clean
    assert faulted["air_temperature"].loc[pd.Timestamp(sc.end), "1"] == clean[
        "air_temperature"
    ].loc[pd.Timestamp(sc.end), "1"]


def test_onset_after_start_for_drift():
    clean = make_clean()
    sc = scen(0, "drift", params={"k": 3.0}, hours=24)  # 0 -> 1.5 degC over 288 steps
    _, labels = inject(suite_of(sc), clean, SIGMA, neighbors=NEIGHBORS)

    start, end, onset = (labels[c].iloc[0] for c in ("start", "end", "onset"))
    assert start < onset < end
    # first step whose ramp value reaches sigma_ref = 0.5: floor(0.5 / (1.5/287)) + 1 = 96
    assert onset == start + 96 * pd.Timedelta(STEP)


def test_onset_edge_cases():
    clean = make_clean()
    suite = suite_of(
        scen(0, "drift", stations=("1",), params={"k": 1.0}, hours=24),  # reaches 1 sigma only at the end
        scen(1, "drift", stations=("2",), params={"k": 0.5}, hours=24),  # never reaches 1 sigma
        scen(2, "offset", stations=("3",)),
    )
    _, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    exactly_one, never, offset = labels.itertuples()
    assert exactly_one.onset == exactly_one.end - pd.Timedelta(STEP)
    assert pd.isna(never.onset)  # NaT, not an index
    assert offset.onset == offset.start  # non-drift families fault from the first step


def test_2023_never_modified():
    clean = make_clean()
    suite = suite_of(
        scen(0, "offset", start=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), hours=12),
        scen(1, "noise", "relative_humidity", ("2",)),
        scen(2, "rain_suppress", "precipitation", ("3",)),
    )
    faulted, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    assert (labels["start"] >= FAULT_FLOOR).all()
    for v in clean:
        in_2023 = clean[v].index < FAULT_FLOOR
        assert in_2023.any()
        assert np.array_equal(
            faulted[v].to_numpy()[in_2023], clean[v].to_numpy()[in_2023], equal_nan=True
        )


def test_scenario_starting_in_2023_is_rejected():
    clean = make_clean()
    bad = scen(0, "offset", start=datetime(2023, 12, 31, 20, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="2023"):
        inject(suite_of(bad), clean, SIGMA, neighbors=NEIGHBORS)


def test_window_off_the_time_grid_is_rejected():
    clean = make_clean()
    bad = scen(0, "offset", start=T0 + timedelta(minutes=2))
    with pytest.raises(ValueError, match="grid"):
        inject(suite_of(bad), clean, SIGMA, neighbors=NEIGHBORS)


def test_overlapping_windows_on_one_station_are_rejected():
    clean = make_clean()
    suite = suite_of(scen(0, "offset"), scen(1, "noise", start=T0 + timedelta(hours=3)))
    with pytest.raises(ValueError, match="overlap"):
        inject(suite, clean, SIGMA, neighbors=NEIGHBORS)


def test_rng_is_seeded_by_suite_seed_and_scenario_index():
    clean = make_clean()
    first = scen(0, "offset", stations=("3",))
    noisy = scen(1, "noise", "relative_humidity", ("2",), hours=3)
    faulted, _ = inject(suite_of(first, noisy, seed=11), clean, SIGMA, neighbors=NEIGHBORS)

    ref = clean["relative_humidity"]["2"]
    win = ref.loc[pd.Timestamp(noisy.start) : pd.Timestamp(noisy.end) - pd.Timedelta(STEP)]
    expected = clip_physical(
        "relative_humidity",
        apply_fault(
            "noise", win.to_numpy().astype(np.float64), noisy.params,
            float(SIGMA.loc["2", "relative_humidity"]), np.random.default_rng([11, 1]), np.nan,
        ),
    ).astype(np.float32)
    got = faulted["relative_humidity"]["2"].loc[win.index].to_numpy()
    assert np.array_equal(got, expected)

    # same inputs -> identical outputs
    again, _ = inject(suite_of(first, noisy, seed=11), clean, SIGMA, neighbors=NEIGHBORS)
    pd.testing.assert_frame_equal(again["relative_humidity"], faulted["relative_humidity"])
    # a different suite seed changes the noise
    other, _ = inject(suite_of(first, noisy, seed=12), clean, SIGMA, neighbors=NEIGHBORS)
    assert not np.array_equal(
        other["relative_humidity"]["2"].to_numpy(), faulted["relative_humidity"]["2"].to_numpy()
    )


def test_clip_applied_after_fault():
    clean = make_clean()
    clean["relative_humidity"].loc[:, "1"] = np.float32(99.0)
    suite = suite_of(
        scen(0, "offset", "relative_humidity", ("1",), params={"k": 5.0}),  # +10 % -> 109
        scen(1, "rain_scale", "precipitation", ("2",), params={"f": 0.5}),
    )
    faulted, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)
    assert faulted["relative_humidity"].to_numpy().max() <= 100.0
    assert faulted["relative_humidity"]["1"].loc[pd.Timestamp(T0)] == 100.0
    assert (faulted["precipitation"].to_numpy() >= 0).all()


def test_nan_cells_stay_nan():
    clean = make_clean()
    t = pd.Timestamp(T0) + pd.Timedelta(hours=1)
    clean["air_temperature"].loc[t, "1"] = np.nan
    faulted, _ = inject(suite_of(scen(0, "offset")), clean, SIGMA, neighbors=NEIGHBORS)
    assert np.isnan(faulted["air_temperature"].loc[t, "1"])


def test_stuck_holds_last_clean_value_before_window():
    clean = make_clean()
    sc = scen(0, "stuck", hours=3)
    faulted, labels = inject(suite_of(sc), clean, SIGMA, neighbors=NEIGHBORS)
    prev = clean["air_temperature"]["1"].loc[pd.Timestamp(T0) - pd.Timedelta(STEP)]
    win = faulted["air_temperature"]["1"].loc[
        pd.Timestamp(sc.start) : pd.Timestamp(sc.end) - pd.Timedelta(STEP)
    ]
    assert (win == prev).all()
    assert len(labels) == 1


def test_stuck_with_nonfinite_prev_is_skipped_with_warning():
    clean = make_clean()
    clean["air_temperature"].loc[pd.Timestamp(T0) - pd.Timedelta(STEP), "1"] = np.nan
    suite = suite_of(scen(0, "stuck", stations=("1",)), scen(1, "stuck", stations=("2",)))

    with pytest.warns(RuntimeWarning, match=suite.scenarios[0].id):
        faulted, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    assert labels["station"].tolist() == ["2"]  # no label row for the skipped scenario
    assert np.array_equal(
        faulted["air_temperature"]["1"].to_numpy(),
        clean["air_temperature"]["1"].to_numpy(),
        equal_nan=True,
    )


def test_size_and_size_phys():
    clean = make_clean()
    suite = suite_of(
        scen(0, "offset", stations=("1",), params={"k": 2.0}),
        scen(1, "spike", stations=("2",), hours=24, params={"k": 4.0, "n": 3.0}),
        scen(2, "flatline", stations=("3",), params={"w_steps": 12.0}),
        scen(3, "stuck", stations=("4",)),
        scen(4, "gain", stations=("5",), params={"g": 1.5}),
        scen(5, "rain_scale", "precipitation", ("1",), params={"f": 2.0}),
        scen(6, "rain_suppress", "precipitation", ("2",)),
        scen(7, "rain_inject", "precipitation", ("3",), hours=3,
             params={"total_mm": 10.0, "duration_steps": 24.0}),
    )
    # precipitation has no sigma column at all: sigma must not be consulted for it
    assert "precipitation" not in SIGMA.columns
    _, labels = inject(suite, clean, SIGMA, neighbors=NEIGHBORS)

    by_family = labels.set_index("family")
    assert by_family.loc["offset", ["size", "size_phys"]].tolist() == [2.0, pytest.approx(1.0)]
    assert by_family.loc["spike", "size"] == 4.0
    assert by_family.loc["spike", "size_phys"] == pytest.approx(4.0 * 0.6)  # k * sigma_ref("2")
    assert by_family.loc["flatline", ["size", "size_phys"]].tolist() == [12.0, 12.0]
    assert by_family.loc["stuck", ["size", "size_phys"]].isna().all()
    assert by_family.loc["gain", ["size", "size_phys"]].tolist() == [1.5, 1.5]
    assert by_family.loc["rain_scale", ["size", "size_phys"]].tolist() == [2.0, 2.0]
    assert by_family.loc["rain_suppress", ["size", "size_phys"]].isna().all()
    assert by_family.loc["rain_inject", ["size", "size_phys"]].tolist() == [10.0, 10.0]
    assert set(by_family.loc[["rain_scale", "rain_suppress", "rain_inject"], "unit"]) == {"mm"}


def test_empty_suite_returns_copies_and_typed_empty_labels():
    clean = make_clean()
    faulted, labels = inject(Suite(name="clean2024", seed=1, scenarios=()), clean, SIGMA)
    assert list(labels.columns) == LABEL_COLUMNS
    assert len(labels) == 0
    for col in ("start", "end", "onset"):
        assert isinstance(labels[col].dtype, pd.DatetimeTZDtype)
    for v in clean:
        pd.testing.assert_frame_equal(faulted[v], clean[v])
        assert faulted[v] is not clean[v]


# --- Added families (15/16/18): realistic_replace, cycle_shaped, network_bias ---

NEW_PARAMS = {
    "realistic_replace": {"k": 1.0, "generator": 0.0},
    "cycle_shaped": {"k": 2.0},
    "network_bias": {"k": 2.0},
}


def test_network_bias_hits_every_listed_station():
    sc = scen(0, "network_bias", stations=("1", "2", "3"), params=NEW_PARAMS["network_bias"])
    _, labels = inject(suite_of(sc), make_clean(), SIGMA)
    assert sorted(labels["station"]) == ["1", "2", "3"]
    assert (labels["n_stations"] == 3).all()
    assert (labels["family"] == "network_bias").all()


def test_realistic_replace_rewrites_window_and_labels_once():
    clean = make_clean()
    sc = scen(0, "realistic_replace", stations=("1",), params=NEW_PARAMS["realistic_replace"])
    faulted, labels = inject(suite_of(sc), clean, SIGMA)
    mask = window_mask(labels, clean["air_temperature"], "air_temperature")
    assert len(labels) == 1 and labels.iloc[0]["size"] == 1.0
    # the window changed, and nothing outside it did
    changed = faulted["air_temperature"].to_numpy() != clean["air_temperature"].to_numpy()
    assert changed[mask].any()
    assert not changed[~mask].any()


def test_cycle_shaped_follows_supplied_diurnal():
    clean = make_clean()
    # a diurnal shape that is 0 for the first half of the window and 1 for the second
    tod = pd.RangeIndex(288, name="tod_bin")
    shape = pd.DataFrame({s: np.where(tod < 144, 0.0, 1.0) for s in STATIONS}, index=tod)
    start = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)  # window spans the 12:00 (bin 144) step up
    sc = scen(0, "cycle_shaped", stations=("1",), start=start, hours=6, params=NEW_PARAMS["cycle_shaped"])
    faulted, labels = inject(suite_of(sc), clean, SIGMA, diurnal={"air_temperature": shape})
    i0 = clean["air_temperature"].index.get_loc(pd.Timestamp(start))
    delta = (faulted["air_temperature"].to_numpy() - clean["air_temperature"].to_numpy())[:, 0]
    win = delta[i0 : i0 + 72]  # 6 h = 72 steps
    assert np.allclose(win.max(), 2.0 * SIGMA.loc["1", "air_temperature"], atol=1e-4)
    assert np.isclose(win[:24].max(), 0.0)  # first 2 h (tod 120..143) are below the diurnal step
