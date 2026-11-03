"""Tests for attacks.grid: expansion of the YAML grid configs into unplaced specs.

All expected counts are written out as arithmetic from the spec section 6 grid table, so a
change to a YAML file that drifts from the spec fails loudly here.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from attacks.grid import GridConfig, expand, load_grid
from attacks.schema import REQUIRED_PARAMS, load_suite

SCENARIOS = Path(__file__).resolve().parents[1] / "attacks" / "scenarios"

VALUE_VARIABLES = {"air_temperature", "relative_humidity"}
N_VALUE_VARS = 2  # air_temperature, relative_humidity


def _grid(name: str) -> GridConfig:
    return load_grid(SCENARIOS / f"grid_{name}.yaml")


# --------------------------------------------------------------------------- counts


def test_expand_counts_dev():
    # (cells per variable, per replicate) from spec section 6, dev columns.
    value_cells = {
        "offset": 5 * 4,  # k 0.5,1,2,3,5  x durations 1,6,24,72 h
        "noise": 5 * 4,  # same grid as offset
        "drift": 4 * 3,  # k 1,2,3,5  x ramp(=window) 24,72,168 h
        "spike": 3 * 3 * 1,  # k 2,4,8  x n 1,3,6  x one fixed 24 h window
        "flatline": 3,  # w 1,3,6 h (window = w)
        "stuck": 4,  # durations 1,6,24,72 h, no size parameter
        "multi_offset": 1 * 4 * 1,  # k 2  x n 2,3,5,8  x one fixed 24 h window
    }
    precip_cells = {
        "rain_scale": 4 * 2,  # f 0.5,1.5,2,3  x durations 6,24 h
        "rain_suppress": 2,  # durations 6,24 h
        "rain_inject": 4 * 2,  # total_mm 2,5,10,20  x duration_steps 1,3 h (= window)
    }
    value_reps, precip_reps = 2, 3

    expected = {f: n * N_VALUE_VARS * value_reps for f, n in value_cells.items()}
    expected |= {f: n * 1 * precip_reps for f, n in precip_cells.items()}
    assert sum(value_cells.values()) == 72
    assert sum(precip_cells.values()) == 18
    assert sum(expected.values()) == 72 * 2 * 2 + 18 * 3 == 342

    specs = expand(_grid("dev"))
    assert len(specs) == 342
    assert Counter(s["family"] for s in specs) == expected


def test_expand_counts_heldout():
    value_cells = {
        "offset": 3 * 3,  # k 0.75,1.5,4  x durations 12,48,120 h
        "noise": 3 * 3,
        "drift": 2 * 2,  # k 1.5,4  x ramp 120,240 h
        "spike": 2 * 2 * 1,  # k 3,6  x n 2,4  x one fixed 48 h window
        "flatline": 2,  # w 2,4 h
        "stuck": 2,  # durations 12,48 h
        "multi_offset": 1 * 2 * 1,  # k 3  x n 4,6  x one fixed 48 h window
        "gain": 3 * 2,  # g 0.5,1.5,2  x durations 24,72 h
        "intermittent_offset": 2 * 2,  # k 1.5,3  x durations 48,120 h
        "value_replay": 2 * 2 * 2,  # days_back 1,7  x noise 0,0.33  x durations 24,48 h
    }
    precip_cells = {
        "rain_scale": 2 * 1,  # f 0.75,2.5  x duration 12 h
        "rain_suppress": 1,  # duration 12 h
        "rain_inject": 2 * 1,  # total_mm 7,15  x duration_steps 2 h
    }
    reps = 2
    expected = {f: n * N_VALUE_VARS * reps for f, n in value_cells.items()}
    expected |= {f: n * 1 * reps for f, n in precip_cells.items()}
    assert sum(value_cells.values()) == 50
    assert sum(precip_cells.values()) == 5
    assert sum(expected.values()) == 50 * 2 * 2 + 5 * 2 == 210

    specs = expand(_grid("heldout"))
    assert len(specs) == 210
    assert Counter(s["family"] for s in specs) == expected


def test_expand_counts_smoke():
    # temperature only, replicates 1, k in {1, 3}
    expected = {
        "offset": 2 * 3,  # k 1,3  x durations 1,6,24 h
        "noise": 2 * 3,
        "drift": 2 * 2,  # k 1,3  x ramp 24,72 h
        "spike": 2 * 3 * 1,  # k 1,3  x n 1,3,6  x one fixed 24 h window
        "flatline": 3,  # w 1,3,6 h
        "stuck": 3,  # durations 1,6,24 h
    }
    cfg = _grid("smoke")
    specs = expand(cfg)
    assert len(specs) == sum(expected.values()) == 28
    assert Counter(s["family"] for s in specs) == expected
    assert {s["variable"] for s in specs} == {"air_temperature"}
    assert cfg.months == [6]
    assert cfg.max_stations == 20
    assert cfg.replicates == 1
    ks = {s["params"]["k"] for s in specs if "k" in s["params"]}
    assert ks == {1.0, 3.0}


# --------------------------------------------------------------------------- shape


@pytest.mark.parametrize("name", ["smoke", "dev", "heldout"])
def test_params_keys_match_required_params(name):
    for spec in expand(_grid(name)):
        assert set(spec) == {"family", "variable", "params", "duration_steps"}
        assert set(spec["params"]) == REQUIRED_PARAMS[spec["family"]], spec
        assert all(isinstance(v, float) for v in spec["params"].values())
        assert isinstance(spec["duration_steps"], int) and spec["duration_steps"] > 0


@pytest.mark.parametrize("name", ["smoke", "dev", "heldout"])
def test_variables_match_family(name):
    for spec in expand(_grid(name)):
        if spec["family"].startswith("rain_"):
            assert spec["variable"] == "precipitation"
        else:
            assert spec["variable"] in VALUE_VARIABLES


def test_durations_converted_to_five_minute_steps():
    specs = expand(_grid("dev"))

    def steps(family):
        return {s["duration_steps"] for s in specs if s["family"] == family}

    assert steps("offset") == {12, 72, 288, 864}  # 1, 6, 24, 72 h
    assert steps("noise") == {12, 72, 288, 864}
    assert steps("stuck") == {12, 72, 288, 864}
    assert steps("drift") == {288, 864, 2016}  # ramp = window = 24, 72, 168 h
    assert steps("spike") == {288}  # single fixed 24 h window
    assert steps("multi_offset") == {288}
    assert steps("rain_scale") == {72, 288}  # 6, 24 h
    assert steps("rain_suppress") == {72, 288}


def test_self_windowed_families_pin_their_own_window():
    for name in ("dev", "heldout"):
        for spec in expand(_grid(name)):
            if spec["family"] == "flatline":
                assert spec["duration_steps"] == spec["params"]["w_steps"]
            if spec["family"] == "rain_inject":
                assert spec["duration_steps"] == spec["params"]["duration_steps"]
    dev = expand(_grid("dev"))
    assert {s["params"]["w_steps"] for s in dev if s["family"] == "flatline"} == {
        12.0,
        36.0,
        72.0,
    }  # 1, 3, 6 h
    assert {
        s["params"]["duration_steps"] for s in dev if s["family"] == "rain_inject"
    } == {12.0, 36.0}  # 1, 3 h


def test_replicates_repeat_identical_specs_and_are_independent_objects():
    specs = expand(_grid("dev"))
    stuck_t = [
        s
        for s in specs
        if s["family"] == "stuck"
        and s["variable"] == "air_temperature"
        and s["duration_steps"] == 12
    ]
    assert len(stuck_t) == 2  # dev replicates
    assert stuck_t[0] == stuck_t[1]
    assert stuck_t[0] is not stuck_t[1]
    assert stuck_t[0]["params"] is not stuck_t[1]["params"]
    rain = [s for s in specs if s["family"] == "rain_suppress" and s["duration_steps"] == 72]
    assert len(rain) == 3  # dev precipitation replicates


def test_expand_is_deterministic():
    assert expand(_grid("dev")) == expand(_grid("dev"))
    assert expand(_grid("heldout")) == expand(_grid("heldout"))


# --------------------------------------------------------------------------- dev vs heldout


def test_heldout_values_disjoint_from_dev():
    dev, held = expand(_grid("dev")), expand(_grid("heldout"))
    assert len(dev) != len(held)  # different sizes (342 vs 210)

    def values(specs, family):
        out: dict[str, set[float]] = {"duration_steps": set()}
        for s in specs:
            if s["family"] != family:
                continue
            out["duration_steps"].add(float(s["duration_steps"]))
            for key, val in s["params"].items():
                out.setdefault(key, set()).add(val)
        return out

    shared = {s["family"] for s in dev} & {s["family"] for s in held}
    assert shared  # sanity: the comparison is not vacuous
    for family in sorted(shared):
        d, h = values(dev, family), values(held, family)
        assert d.keys() == h.keys(), family
        for key in d:
            assert not (d[key] & h[key]), f"{family}.{key} shares {d[key] & h[key]}"


def test_heldout_only_families_absent_from_dev():
    held_only = {"gain", "intermittent_offset"}
    dev_cfg, held_cfg, smoke_cfg = _grid("dev"), _grid("heldout"), _grid("smoke")
    assert not held_only & set(dev_cfg.families)
    assert not held_only & set(smoke_cfg.families)
    assert held_only <= set(held_cfg.families)
    assert not held_only & {s["family"] for s in expand(dev_cfg)}
    assert held_only <= {s["family"] for s in expand(held_cfg)}


def test_heldout_only_family_rejected_in_dev_config():
    with pytest.raises(ValidationError, match="heldout-only"):
        GridConfig(
            name="bad",
            seed=1,
            split="dev",
            variables=["air_temperature"],
            station_pool="dev",
            replicates=1,
            families={"gain": {"g": [1.5]}},
            durations_h=[24],
        )


def test_unknown_or_missing_family_key_rejected():
    base = dict(
        name="bad",
        seed=1,
        split="dev",
        variables=["air_temperature"],
        station_pool="dev",
        replicates=1,
        durations_h=[24],
    )
    with pytest.raises(ValidationError, match="unexpected"):
        GridConfig(**base, families={"offset": {"k": [1], "bogus": [1]}})
    with pytest.raises(ValidationError, match="missing"):
        GridConfig(**base, families={"offset": {}})
    with pytest.raises(ValidationError, match="ramp_h"):
        GridConfig(**base, families={"drift": {"k": [1]}})


def test_pools_and_months_for_each_grid():
    assert _grid("dev").station_pool == "dev"
    assert _grid("heldout").station_pool == "heldout"
    assert _grid("dev").split == "dev" and _grid("heldout").split == "heldout"
    assert _grid("dev").replicates == 2 and _grid("dev").precip_replicates == 3
    assert _grid("heldout").replicates == 2


# --------------------------------------------------------------------------- showcase


def test_showcase_loads_and_covers_all_families():
    suite = load_suite(SCENARIOS / "showcase.yaml")
    assert suite.name == "showcase"
    # Every implemented family appears (realistic_replace / cycle_shaped / network_bias
    # are added by later tasks and are not in schema.Family yet).
    assert {s.family for s in suite.scenarios} == set(REQUIRED_PARAMS)
    assert len(suite.scenarios) == len(REQUIRED_PARAMS)

    ids = [s.id for s in suite.scenarios]
    assert len(set(ids)) == len(ids)

    for s in suite.scenarios:
        assert s.split == "showcase"
        assert s.stations[0] == "77", s.id
        # July 2024, tz-aware, on the 5-minute grid
        assert s.start.year == 2024 and s.start.month == 7, s.id
        assert s.end <= datetime(2024, 8, 1, tzinfo=timezone.utc), s.id
        for t in (s.start, s.end):
            assert t.utcoffset() == timedelta(0)
            assert t.second == 0 and t.minute % 5 == 0

    by_family = {s.family: s for s in suite.scenarios}
    mo = by_family["multi_offset"]
    assert len(mo.stations) == int(mo.params["n"])
    assert len(set(mo.stations)) == len(mo.stations)
    # every single-station scenario is on station 77 alone (multi_offset and network_bias
    # deliberately span a cluster)
    for s in suite.scenarios:
        if s.family not in ("multi_offset", "network_bias"):
            assert s.stations == ("77",), s.id
    assert by_family["network_bias"].stations[0] == "77"

    # the flatline window is exactly w_steps long; rain_inject window = duration_steps
    step = timedelta(minutes=5)
    fl = by_family["flatline"]
    assert (fl.end - fl.start) / step == fl.params["w_steps"]
    ri = by_family["rain_inject"]
    assert (ri.end - ri.start) / step == ri.params["duration_steps"]


def test_showcase_windows_do_not_overlap_per_variable():
    suite = load_suite(SCENARIOS / "showcase.yaml")
    by_var: dict[str, list] = {}
    for s in suite.scenarios:
        by_var.setdefault(s.variable, []).append(s)
    for var, scs in by_var.items():
        scs = sorted(scs, key=lambda s: s.start)
        for a, b in zip(scs, scs[1:]):
            assert a.end <= b.start, f"{var}: {a.id} overlaps {b.id}"
