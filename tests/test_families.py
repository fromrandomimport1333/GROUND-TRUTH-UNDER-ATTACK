"""Category-A value fault families: one small test per behaviour."""

import numpy as np
import pytest

from attacks.families import apply_fault, clip_physical, delta_onset


def rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def f32(*values: float) -> np.ndarray:
    return np.array(values, dtype=np.float32)


def zeros(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.float32)


# Every implemented family with params that are valid for a 48-step window.
ALL_CASES = [
    ("offset", {"k": 2.0}),
    ("drift", {"k": 3.0}),
    ("spike", {"k": 4.0, "n": 3.0}),
    ("noise", {"k": 1.0}),
    ("flatline", {"w_steps": 5.0}),
    ("stuck", {}),
    ("multi_offset", {"k": 2.0, "n": 3.0}),
    ("gain", {"g": 1.5}),
    ("intermittent_offset", {"k": 2.0}),
    ("rain_scale", {"f": 2.0}),
    ("rain_suppress", {}),
    ("rain_inject", {"total_mm": 5.0, "duration_steps": 6.0}),
]
STOCHASTIC_CASES = [c for c in ALL_CASES if c[0] in {"spike", "noise", "rain_inject"}]


def window() -> np.ndarray:
    return rng(123).random(48).astype(np.float32) + 0.1


def test_offset_adds_k_sigma():
    x = f32(1.0, 2.0, 3.0, 4.0)
    out = apply_fault("offset", x, {"k": 2.0}, 0.5, rng(), 0.0)
    np.testing.assert_allclose(out - x, 1.0)


def test_multi_offset_is_per_station_offset():
    x = f32(1.0, 2.0, 3.0)
    out = apply_fault("multi_offset", x, {"k": 2.0, "n": 3.0}, 0.5, rng(), 0.0)
    np.testing.assert_allclose(out - x, 1.0)


def test_drift_starts_at_zero_ends_at_k_sigma():
    x = zeros(25)
    out = apply_fault("drift", x, {"k": 3.0}, 0.5, rng(), 0.0)
    delta = out - x
    assert delta[0] == 0.0
    assert delta[-1] == pytest.approx(1.5)
    np.testing.assert_allclose(np.diff(delta), 1.5 / 24, rtol=1e-4)


def test_spike_changes_exactly_n_points():
    x = zeros(60)
    out = apply_fault("spike", x, {"k": 4.0, "n": 3.0}, 0.5, rng(7), 0.0)
    changed = np.flatnonzero(out != x)
    assert len(changed) == 3
    np.testing.assert_allclose(np.abs(out[changed]), 2.0)


def test_spike_signs_are_random_and_positions_isolated():
    x = zeros(200)
    out = apply_fault("spike", x, {"k": 1.0, "n": 20.0}, 1.0, rng(3), 0.0)
    changed = np.flatnonzero(out != x)
    assert len(changed) == 20
    assert (out[changed] > 0).any() and (out[changed] < 0).any()
    assert np.all(np.diff(changed) >= 2)  # single-step pulses, never merged


def test_spike_with_more_pulses_than_fit_still_changes_distinct_points():
    x = zeros(4)
    out = apply_fault("spike", x, {"k": 1.0, "n": 4.0}, 1.0, rng(), 0.0)
    assert np.count_nonzero(out != x) == 4


def test_noise_std_matches_k_sigma():
    x = zeros(20_000)
    out = apply_fault("noise", x, {"k": 2.0}, 0.5, rng(1), 0.0)
    delta = (out - x).astype(np.float64)
    assert delta.std() == pytest.approx(1.0, rel=0.03)
    assert abs(delta.mean()) < 0.05


def test_flatline_reduces_variance():
    x = rng(5).normal(0.0, 1.0, 600).astype(np.float32)
    out = apply_fault("flatline", x, {"w_steps": 12.0}, 1.0, rng(), 0.0)
    assert out.var() < 0.2 * x.var()
    # centred mean away from the edges; edges use the shorter available window
    np.testing.assert_allclose(out[300], x[294:306].mean(), rtol=1e-4, atol=1e-5)
    np.testing.assert_allclose(out[0], x[:6].mean(), rtol=1e-4, atol=1e-5)


def test_flatline_keeps_missing_cells_missing():
    x = f32(1.0, np.nan, 3.0, 4.0)
    out = apply_fault("flatline", x, {"w_steps": 3.0}, 1.0, rng(), 0.0)
    assert np.isnan(out[1])
    assert np.isfinite(out[[0, 2, 3]]).all()


def test_stuck_holds_prev():
    x = f32(1.0, 2.0, 3.0, 4.0)
    out = apply_fault("stuck", x, {}, 1.0, rng(), 9.0)
    np.testing.assert_array_equal(out, f32(9.0, 9.0, 9.0, 9.0))


def test_stuck_requires_finite_prev():
    with pytest.raises(ValueError, match="prev"):
        apply_fault("stuck", f32(1.0, 2.0), {}, 1.0, rng(), float("nan"))


def test_gain_preserves_window_mean():
    x = rng(2).normal(10.0, 2.0, 100).astype(np.float32)
    out = apply_fault("gain", x, {"g": 1.5}, 1.0, rng(), 0.0)
    assert out.mean() == pytest.approx(x.mean(), rel=1e-5)
    assert out.std() == pytest.approx(1.5 * x.std(), rel=1e-4)


def test_intermittent_offset_alternates_hourly():
    x = zeros(48)
    out = apply_fault("intermittent_offset", x, {"k": 2.0}, 0.5, rng(), 0.0)
    delta = out - x
    np.testing.assert_allclose(delta[:12], 1.0)   # first hour faulted
    np.testing.assert_allclose(delta[12:24], 0.0)  # second hour clean
    np.testing.assert_allclose(delta[24:36], 1.0)
    np.testing.assert_allclose(delta[36:], 0.0)


def test_rain_scale_keeps_dry_steps_dry():
    x = f32(0.0, 0.2, 0.0, 0.5, 0.0)
    out = apply_fault("rain_scale", x, {"f": 2.0}, 1.0, rng(), 0.0)
    np.testing.assert_allclose(out, f32(0.0, 0.4, 0.0, 1.0, 0.0), rtol=1e-6)
    assert np.all(out[x == 0.0] == 0.0)


def test_rain_suppress_zeroes_window():
    x = f32(0.0, 0.2, 1.0, 0.5, 0.0)
    out = apply_fault("rain_suppress", x, {}, 1.0, rng(), 0.0)
    np.testing.assert_array_equal(out, zeros(5))


def test_rain_inject_adds_exact_total():
    x = zeros(60)
    out = apply_fault(
        "rain_inject", x, {"total_mm": 10.0, "duration_steps": 12.0}, 1.0, rng(4), 0.0
    )
    delta = (out - x).astype(np.float64)
    assert delta.sum() == pytest.approx(10.0, abs=1e-5)
    wet = np.flatnonzero(delta > 0)
    assert len(wet) == 12
    assert np.array_equal(wet, np.arange(wet[0], wet[0] + 12))  # one contiguous event
    assert np.all(delta >= 0)
    # triangular, symmetric, peaking mid-event
    event = delta[wet[0] : wet[0] + 12]
    np.testing.assert_allclose(event, event[::-1], rtol=1e-5)
    assert np.argmax(event) in (5, 6)
    assert event[0] < event[3] < event[5]


def test_rain_inject_start_is_random_inside_window():
    x = zeros(60)
    starts = set()
    for seed in range(40):
        out = apply_fault(
            "rain_inject", x, {"total_mm": 5.0, "duration_steps": 12.0}, 1.0, rng(seed), 0.0
        )
        wet = np.flatnonzero(out > 0)
        assert wet[0] >= 0 and wet[-1] < 60
        starts.add(int(wet[0]))
    assert len(starts) > 5


def test_rain_inject_longer_than_window_is_clamped_to_window():
    x = zeros(6)
    out = apply_fault(
        "rain_inject", x, {"total_mm": 3.0, "duration_steps": 12.0}, 1.0, rng(), 0.0
    )
    assert out.astype(np.float64).sum() == pytest.approx(3.0, abs=1e-5)


def test_rh_clipped_to_physical_range():
    x = f32(99.0, 50.0, 1.0)
    out = apply_fault("offset", x, {"k": 2.0}, 2.5, rng(), 0.0)  # +5
    assert out.max() > 100.0  # the raw fault is out of range ...
    clipped = clip_physical("relative_humidity", out)
    assert clipped[0] == 100.0 and clipped.max() <= 100.0  # ... clipping fixes it
    neg = clip_physical("relative_humidity", f32(-3.0, 40.0))
    np.testing.assert_array_equal(neg, f32(0.0, 40.0))


def test_precipitation_clipped_non_negative_and_temperature_untouched():
    p = clip_physical("precipitation", f32(-0.5, 0.0, 1.0))
    np.testing.assert_array_equal(p, f32(0.0, 0.0, 1.0))
    t = f32(-40.0, 120.0)
    out = clip_physical("air_temperature", t)
    np.testing.assert_array_equal(out, t)
    assert out is not t
    with pytest.raises(ValueError):
        clip_physical("pressure", t)


def test_clip_keeps_missing_and_dtype():
    x = f32(np.nan, 101.0)
    out = clip_physical("relative_humidity", x)
    assert out.dtype == np.float32
    assert np.isnan(out[0]) and out[1] == 100.0


@pytest.mark.parametrize(("family", "params"), STOCHASTIC_CASES)
def test_same_seed_same_output(family, params):
    x = window()
    a = apply_fault(family, x, params, 0.7, rng(11), 0.3)
    b = apply_fault(family, x, params, 0.7, rng(11), 0.3)
    np.testing.assert_array_equal(a, b)


@pytest.mark.parametrize(("family", "params"), STOCHASTIC_CASES)
def test_different_seed_different_output(family, params):
    x = window()
    a = apply_fault(family, x, params, 0.7, rng(11), 0.3)
    b = apply_fault(family, x, params, 0.7, rng(12), 0.3)
    assert not np.array_equal(a, b)


@pytest.mark.parametrize(("family", "params"), ALL_CASES)
def test_input_not_mutated(family, params):
    x = window()
    before = x.copy()
    out = apply_fault(family, x, params, 0.7, rng(), 0.3)
    np.testing.assert_array_equal(x, before)
    assert out is not x
    assert not np.shares_memory(out, x)
    assert out.shape == x.shape
    assert out.dtype == np.float32


def test_unknown_family_is_rejected():
    with pytest.raises(ValueError, match="nonsense"):
        apply_fault("nonsense", zeros(4), {"k": 1.0}, 1.0, rng(), 0.0)


def test_context_family_without_context_is_rejected():
    for name in ("realistic_replace", "cycle_shaped"):
        with pytest.raises(ValueError, match=name):
            apply_fault(name, zeros(4), {"k": 1.0, "generator": 0.0}, 1.0, rng(), 0.0)


def test_delta_onset_drift_is_first_step_reaching_one_sigma():
    x = zeros(101)
    out = apply_fault("drift", x, {"k": 4.0}, 0.5, rng(), 0.0)  # ramp 0 -> 2.0
    onset = delta_onset("drift", out - x, 0.5)
    assert onset == 25  # delta[i] = 2.0 * i / 100 >= 0.5  <=>  i >= 25
    assert abs((out - x)[onset]) >= 0.5 > abs((out - x)[onset - 1])


def test_delta_onset_is_zero_for_non_drift_families():
    delta = f32(1.0, 1.0, 1.0)
    assert delta_onset("offset", delta, 0.5) == 0
    assert delta_onset("spike", delta, 0.5) == 0


def test_delta_onset_when_ramp_never_reaches_sigma_is_window_length():
    delta = np.linspace(0.0, 0.4, 20, dtype=np.float32)
    assert delta_onset("drift", delta, 1.0) == 20


# --- Added families (Tasks 15, 16, 18): realistic_replace, cycle_shaped, network_bias ---

from attacks.families import cycle_shaped, realistic_replace  # noqa: E402


def test_realistic_replace_matches_marginal_mean():
    # history around 10.0; window irrelevant to the output level
    history = rng(1).normal(10.0, 1.0, size=288 * 30).astype(np.float32)
    x = np.full(288, 50.0, dtype=np.float32)  # true window is nothing like history
    tod = np.arange(288)
    out = realistic_replace(x, {"k": 2.0, "generator": 0.0}, sigma=1.0, rng=rng(3),
                            history=history, tod_index=tod)
    assert abs(out.mean() - (history.mean() + 2.0)) < 0.1 * abs(history.mean())


def test_realistic_replace_diverges_from_neighbours():
    # history (and therefore the generated values) sit near 10; the real window and the
    # neighbour both moved to ~30. The mimic ignores the regional move, so it diverges.
    history = rng(1).normal(10.0, 1.0, size=288 * 30).astype(np.float32)
    x = np.full(288, 30.0, dtype=np.float32)
    neighbour = np.full(288, 30.0, dtype=np.float32)
    out = realistic_replace(x, {"k": 0.0, "generator": 0.0}, sigma=1.0, rng=rng(3),
                            history=history, tod_index=np.arange(288))
    assert np.abs(out - neighbour).mean() > np.abs(x - neighbour).mean()


def test_realistic_replace_input_not_mutated():
    x = np.full(288, 50.0, dtype=np.float32)
    before = x.copy()
    realistic_replace(x, {"k": 1.0, "generator": 1.0}, sigma=1.0, rng=rng(3),
                      history=rng(1).normal(10, 1, 288 * 30).astype(np.float32),
                      tod_index=np.arange(288))
    assert np.array_equal(x, before)


def test_realistic_replace_same_seed_same_output():
    kw = dict(history=rng(1).normal(10, 1, 288 * 30).astype(np.float32), tod_index=np.arange(288))
    x = np.full(288, 50.0, dtype=np.float32)
    a = realistic_replace(x, {"k": 1.0, "generator": 0.0}, 1.0, rng(5), **kw)
    b = realistic_replace(x, {"k": 1.0, "generator": 0.0}, 1.0, rng(5), **kw)
    assert np.array_equal(a, b)


def test_cycle_shaped_peaks_with_diurnal():
    x = np.zeros(288, dtype=np.float32)
    diurnal = np.linspace(0.0, 1.0, 288).astype(np.float32)  # rises across the window
    out = cycle_shaped(x, {"k": 3.0}, sigma=2.0, rng=rng(0), diurnal=diurnal)
    delta = out - x
    assert delta.argmax() == diurnal.argmax()
    assert abs(delta.max() - 3.0 * 2.0) < 1e-4  # k*sigma at the diurnal peak (=1.0)


def test_cycle_shaped_zero_amplitude_is_noop():
    x = np.arange(48, dtype=np.float32)
    out = cycle_shaped(x, {"k": 3.0}, sigma=2.0, rng=rng(0), diurnal=np.zeros(48, dtype=np.float32))
    assert np.array_equal(out, x)


def test_cycle_shaped_input_not_mutated():
    x = np.arange(48, dtype=np.float32)
    before = x.copy()
    cycle_shaped(x, {"k": 3.0}, sigma=2.0, rng=rng(0), diurnal=np.linspace(0, 1, 48).astype(np.float32))
    assert np.array_equal(x, before)


def test_network_bias_is_plain_offset_at_station_level():
    x = f32(1, 2, 3, 4)
    out = apply_fault("network_bias", x, {"k": 2.0}, sigma=0.5, rng=rng(0), prev=0.0)
    assert np.allclose(out, x + 1.0)  # fan-out across stations is the injector's job


from attacks.families import value_replay  # noqa: E402


def test_value_replay_copies_a_real_past_window():
    history = np.arange(288 * 10, dtype=np.float32)  # strictly increasing: each day differs
    x = np.full(288, 999.0, dtype=np.float32)
    out = value_replay(x, {"days_back": 7.0, "noise": 0.0}, sigma=1.0, rng=rng(0),
                       history=history, tod_index=np.arange(288))
    # 7 days back from the end of history, same length -> exact verbatim copy
    start = len(history) - 7 * 288
    assert np.array_equal(out, history[start:start + 288])


def test_value_replay_noise_adds_scatter_but_keeps_mean():
    history = np.full(288 * 10, 20.0, dtype=np.float32)
    x = np.full(288, 0.0, dtype=np.float32)
    out = value_replay(x, {"days_back": 1.0, "noise": 0.5}, sigma=2.0, rng=rng(1),
                       history=history, tod_index=np.arange(288))
    assert abs(out.mean() - 20.0) < 0.2 and out.std() > 0.5  # scattered around the replayed level


def test_value_replay_input_not_mutated():
    x = np.full(288, 5.0, dtype=np.float32)
    before = x.copy()
    value_replay(x, {"days_back": 1.0, "noise": 0.0}, 1.0, rng(0),
                 history=np.arange(288 * 5, dtype=np.float32), tod_index=np.arange(288))
    assert np.array_equal(x, before)
