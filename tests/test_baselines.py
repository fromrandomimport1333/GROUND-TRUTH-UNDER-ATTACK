"""Reference baselines (plan Task 10): simple floors, not the team detector."""

import numpy as np
import pandas as pd

from eval.baselines import neighbor_baseline, rule_baseline


def _index(n):
    return pd.date_range("2024-06-01", periods=n, freq="5min", tz="UTC")


def _frame(columns, n=None):
    n = n or len(next(iter(columns.values())))
    return pd.DataFrame({k: np.asarray(v, dtype="float32") for k, v in columns.items()}, index=_index(n))


def _hits(alarm, column):
    return list(np.flatnonzero(alarm[column].to_numpy()))


# ------------------------------------------------------------------------------ rules


def test_rule_flags_out_of_range():
    # slow 0.5-unit ramps so only the range rule can fire (no step, no constancy)
    t = _frame({"hot": [44, 44.5, 45, 45.5, 46], "cold": [-29, -29.5, -30, -30.5, -31]})
    alarm = rule_baseline(t, "air_temperature")
    assert _hits(alarm, "hot") == [3, 4]  # bounds [-30, 45] are inclusive
    assert _hits(alarm, "cold") == [3, 4]

    rh = _frame({"wet": [98, 99, 100, 100.5, 101], "dry": [2, 1, 0, -1, -2]})
    alarm = rule_baseline(rh, "relative_humidity")
    assert _hits(alarm, "wet") == [3, 4]
    assert _hits(alarm, "dry") == [3, 4]

    p = _frame({"a": [0, 10, 30, 30.5, 31, -0.5], "b": [0, np.nan, 0, 0, 0, 0]})
    alarm = rule_baseline(p, "precipitation")
    assert _hits(alarm, "a") == [3, 4, 5]
    assert _hits(alarm, "b") == []  # missing is not an alarm
    assert alarm.dtypes.eq(bool).all()


def test_rule_flags_flat_temperature():
    rng = np.random.default_rng(0)
    noise = rng.normal(15, 1, 80)  # float draws: consecutive values never repeat
    long_flat = noise.copy()
    long_flat[20:50] = 12.5  # 30 identical readings starting at index 20
    short_flat = noise.copy()
    short_flat[20:44] = 12.5  # 24 identical readings = 23 unchanged steps
    t = _frame({"long": long_flat, "short": short_flat})
    # 12.5 is within 5 degC of the noise, so only the constancy rule can fire
    assert np.abs(np.diff(t.to_numpy(), axis=0)).max() < 5

    alarm = rule_baseline(t, "air_temperature")
    # unchanged for 24 steps (2 h) first holds at the 25th identical reading, and stays on
    assert _hits(alarm, "long") == list(range(44, 50))
    assert _hits(alarm, "short") == []

    # RH plateaus at exactly 100 % are normal (saturation) and are exempt; other plateaus are not
    rh_noise = rng.normal(80, 2, 80)
    sat, stuck = rh_noise.copy(), rh_noise.copy()
    sat[20:60] = 100.0
    stuck[20:60] = 75.0
    rh = _frame({"sat": sat, "stuck": stuck})
    alarm = rule_baseline(rh, "relative_humidity")
    # skip the 80 -> 100 entry/exit jumps (idx 20 and 60), which can trip the step rule
    assert not alarm["sat"].iloc[21:60].any()
    assert _hits(alarm, "stuck") == list(range(44, 60))


def test_rule_flags_step_change():
    t = _frame({"a": [10, 10.5, 16.5, 16.0, 11.0, 6.2, 6.1]})  # +6, -5.0 (boundary), -4.8
    assert _hits(rule_baseline(t, "air_temperature"), "a") == [2]  # strictly greater than 5
    rh = _frame({"a": [50, 52, 73, 72, 54, 52]})  # +21, then -18
    assert _hits(rule_baseline(rh, "relative_humidity"), "a") == [2]
    p = _frame({"a": [0, 0, 29, 0, 0]})  # no step rule for rain
    assert _hits(rule_baseline(p, "precipitation"), "a") == []


# ------------------------------------------------------------------------- neighbours

COLS = ["a", "b", "c", "d", "e", "f"]
NEIGHBORS = {c: [o for o in COLS if o != c][:5] for c in COLS}
SIGMA = pd.Series(0.5, index=COLS)  # z=4 -> alarm above 2.0 degC of disagreement


def _field(n=60):
    base = 15 + 3 * np.sin(np.linspace(0, 6, n))
    return _frame({c: base for c in COLS}, n)


def test_neighbor_flags_persistent_offset():
    x = _field()
    x.loc[x.index[20:30], "a"] += 3.0  # 6 sigma_ref for 10 steps
    alarm = neighbor_baseline(x, NEIGHBORS, SIGMA, z=4.0, persist=3)
    # causal persistence: the alarm rises once 3 consecutive steps disagree, then holds to the end
    assert _hits(alarm, "a") == list(range(22, 30))
    assert not alarm[COLS[1:]].any().any()  # one faulted station does not move a 5-NN median
    assert alarm.shape == x.shape and alarm.dtypes.eq(bool).all()
    assert alarm.index.equals(x.index) and list(alarm.columns) == COLS


def test_neighbor_ignores_single_step_blip():
    x = _field()
    x.loc[x.index[25], "a"] += 10.0
    assert not neighbor_baseline(x, NEIGHBORS, SIGMA, z=4.0, persist=3).any().any()

    x.loc[x.index[26], "a"] += 10.0  # two consecutive steps is still below persist=3
    assert not neighbor_baseline(x, NEIGHBORS, SIGMA, z=4.0, persist=3).any().any()

    # the persistence knob is real: persist=1 reacts to the blip
    assert _hits(neighbor_baseline(x, NEIGHBORS, SIGMA, z=4.0, persist=1), "a") == [25, 26]


def test_neighbor_threshold_is_z_times_sigma_and_two_sided():
    x = _field()
    x.loc[x.index[10:20], "a"] += 1.9  # below z*sigma = 2.0
    x.loc[x.index[30:40], "b"] -= 2.5  # above, negative direction
    alarm = neighbor_baseline(x, NEIGHBORS, SIGMA, z=4.0, persist=3)
    assert _hits(alarm, "a") == []
    assert _hits(alarm, "b") == list(range(32, 40))


def test_neighbor_handles_missing_values_and_unknown_stations():
    x = _field()
    x.loc[x.index[20:30], "a"] += 3.0
    x.loc[x.index[24], "a"] = np.nan  # a gap breaks the consecutive run
    x["z"] = 1.0  # station with no neighbours listed: never alarms
    alarm = neighbor_baseline(x, NEIGHBORS, SIGMA, z=4.0, persist=3)
    assert _hits(alarm, "a") == [22, 23, 27, 28, 29]
    assert not alarm["z"].any()


def test_neighbor_precipitation_hourly_sum_rule():
    n = 48
    zero = np.zeros(n)
    inject = zero.copy()
    inject[10:22] = 1.0  # 12 mm in one hour while all neighbours are dry
    p = _frame({c: zero for c in COLS[1:]} | {"a": inject}, n)[COLS]
    alarm = neighbor_baseline(p, NEIGHBORS, None, variable="precipitation")
    # trailing 12-step (1 h) sum: 1 mm at idx 10, 2 mm at 11 (not > 2), 3 mm at 12 ... 12 mm at 21,
    # then it decays by 1 mm per step and is still > 2 mm through idx 30
    assert _hits(alarm, "a") == list(range(12, 31))
    assert not alarm[COLS[1:]].any().any()


def test_neighbor_precipitation_agreement_and_underreading():
    n = 48
    shower = np.zeros(n)
    shower[10:22] = 1.0
    # everyone sees the same shower: no alarm
    same = _frame({c: shower for c in COLS}, n)
    assert not neighbor_baseline(same, NEIGHBORS, None, variable="precipitation").any().any()
    # the plan's rule |S - median| > max(2 mm, 3 * median) only ever fires on over-reading:
    # a gauge that reads 0 while neighbours see rain has |S - M| = M <= max(2, 3M)
    low = same.copy()
    low["a"] = 0.0
    assert not neighbor_baseline(low, NEIGHBORS, None, variable="precipitation").any().any()
    # over-reading beyond 3x the median (+ more than 2 mm) is flagged
    high = same.copy()
    high["a"] = high["a"] * 6.0  # 72 mm/h against neighbours' 12 mm/h
    assert neighbor_baseline(high, NEIGHBORS, None, variable="precipitation")["a"].any()
