"""Detector invariants: causality, no label leakage, missing data, attacks, calibration, alert rule, metrics."""
import numpy as np
import pytest
import xarray as xr

import core.detection.data as data_module
from core.detection.attacks import inject
from core.detection.features import FeatureExtractor, calendar, capped_weights, history_complete, references, rolling, rule_scores, past_mean
from core.detection.metrics import event_metrics, point_counts, summarize_counts
from core.detection.model import alert_code, confirmed, p_values
from core.detection.utils import config


# ---------------------------------------------------------------- data

def test_netcdf_decoding_paired_cells_units_and_interval_ownership(tmp_path, monkeypatch):
    starts = np.datetime64("2023-01-01", "ns") + np.arange(64)*np.timedelta64(30, "m")
    times = starts+np.timedelta64(30, "m")
    # Distinct x/y values make a Cartesian-product indexing bug detectable.
    values = np.broadcast_to(273.15 + np.arange(36).reshape(1, 6, 6), (64, 6, 6)).copy()
    flags = np.zeros_like(values)
    flags[3, 2, 4] = 101
    ds = xr.Dataset({"T_z300m_HHM": (("time", "Y", "X"), values, {"units": "K"}),
                     "RH_HHM": (("time", "Y", "X"), np.ones_like(values)*60, {"units": "%"}),
                     "Precip_HHS": (("time", "Y", "X"), np.ones_like(values)*2, {"units": "mm"}),
                     **{name: (("time", "Y", "X"), flags.copy(), {"_FillValue": 101.}) for name in ("Flagged_T", "Flagged_RH", "Flagged_Precip")},
                     "time_bnds": (("time", "bounds"), np.stack([starts, times], axis=1))},
                    coords={"time": times, "Y": np.arange(6)*100., "X": np.arange(6)*100.})
    ds.to_netcdf(tmp_path/"source_2023-01.nc", engine="h5netcdf")
    cfg = {**config("data"), "source_glob": "*.nc", "stride": 1, "margin": 0}
    monkeypatch.setattr(data_module, "ROOT", tmp_path)
    assert data_module.extract(cfg)["selected_sites"] == 36
    data = data_module.load_partition("train", cfg)
    assert data.values.shape == (64, 36, 3)
    np.testing.assert_allclose(data.values[0, :, 0], np.arange(36), atol=3e-5)   # kelvin -> degC, paired cells
    assert np.isnan(data.flags[3, 16, 0])                                         # fill value 101 -> missing
    assert data.times[47] == np.datetime64("2023-01-02")                          # timestamps are interval ends
    assert data.starts[47] == np.datetime64("2023-01-01T23:30")


# ---------------------------------------------------------------- causal features

def test_strict_history_excludes_current():
    x = np.arange(20, dtype=float).reshape(20, 1, 1)
    median, *_ = rolling(x, 6, strict=True)
    x[6] = 9000
    assert median[6, 0, 0] == rolling(x, 6, strict=True)[0][6, 0, 0] == 2.5


def test_missing_history_recovery_exact():
    x = np.ones((110, 1, 1))
    x[50] = np.nan
    valid = history_complete(x)
    assert valid[50, 0, 0] and not valid[51:99].any() and valid[99, 0, 0]


def test_leap_year_calendar_fraction():
    x = calendar(np.array(["2024-07-02T00:00", "2023-07-02T12:00"], dtype="datetime64[ns]"))
    np.testing.assert_allclose(x[:, 3], 0, atol=1e-12)
    np.testing.assert_allclose(x[:, 4], -1)


def test_residual_mean_is_strictly_past():
    x = np.arange(8, dtype=float)[:, None, None]
    changed = x.copy(); changed[4:] = 1000
    assert past_mean(x, 3)[4, 0, 0] == past_mean(changed, 3)[4, 0, 0] == 2


def test_no_future_values_affect_earlier_features(extractor, data):
    first = extractor.model_inputs(data)
    altered = data.values.copy()
    altered[80:] += 200
    second = extractor.model_inputs(data.with_values(altered))
    np.testing.assert_allclose(first["features"][:80], second["features"][:80], equal_nan=True)
    np.testing.assert_array_equal(first["valid"][:80], second["valid"][:80])


def test_neighbors_exclude_target_and_use_attacked_values(extractor, data):
    assert not (extractor.graph["near"] == np.arange(36)[:, None]).any()
    base = references(data.values, data.flags, extractor.graph, extractor.cfg)
    altered = data.values.copy()
    altered[:, 0] = 999
    np.testing.assert_allclose(base["near"][:, 0], references(altered, data.flags, extractor.graph, extractor.cfg)["near"][:, 0])
    altered[:, extractor.graph["near"][0], 0] += 20
    assert np.all(references(altered, data.flags, extractor.graph, extractor.cfg)["near"][:, 0, 0] > base["near"][:, 0, 0])


def test_weights_capped_without_losing_mass():
    weights = capped_weights(np.array([1, 100, 100]), np.ones((1, 3), dtype=bool), .45)
    assert weights.max() <= .4500001
    np.testing.assert_allclose(weights.sum(), 1)


def test_missing_target_is_not_scored(extractor, data):
    values = data.values.copy()
    values[60, 0, 0] = np.nan
    assert not extractor.model_inputs(data.with_values(values))["valid"][60, 0, 0]


def test_dry_rain_and_saturated_humidity_are_not_freeze_evidence(extractor):
    raw = np.zeros((1, 36, 3))
    raw[..., 1] = 100
    score, violations = rule_scores(raw, np.zeros_like(raw), np.full_like(raw, 10000), np.zeros_like(raw), extractor.params)
    assert (score[..., 1:] == 0).all() and (violations == 0).all()


def test_fit_rejects_non_training_data(extractor, data):
    with pytest.raises(ValueError):
        extractor.fit(data)


def test_save_load_round_trip(extractor, data, tmp_path):
    extractor.save(tmp_path/"fx.joblib")
    restored = FeatureExtractor.load(tmp_path/"fx.joblib")
    np.testing.assert_allclose(extractor.model_inputs(data)["features"], restored.model_inputs(data)["features"], equal_nan=True)


# ---------------------------------------------------------------- attacks

def test_attack_is_copy_on_write_and_seeded(data):
    original = data.values.copy()
    spec = [{"family": "noise", "start": 50, "end": 80, "sites": [0, 1], "variable": 0}]
    a, b = inject(data.values, spec, 42), inject(data.values, spec, 42)
    np.testing.assert_array_equal(original, data.values)
    np.testing.assert_array_equal(a.observed, b.observed)


def test_labels_count_only_actual_changes():
    injection = inject(np.zeros((20, 1, 3)), [{"family": "rain_scale", "start": 5, "end": 15, "sites": [0], "variable": 2, "factor": 0}], 3)
    assert injection.attempted.sum() == 10 and not injection.changed.any() and injection.events[0]["zero_impact"]


# ---------------------------------------------------------------- calibration and alert rule

def test_tail_values_are_conservative_and_missing_stays_missing():
    tails = [np.array([1, 2, 2, 3], dtype="float32")] * 3
    p = p_values(np.array([[[2., 4., np.nan]]]), tails)
    np.testing.assert_allclose(p[0, 0, :2], [.8, .2])
    assert np.isnan(p[0, 0, 2])


def test_confirmation_never_backdates():
    candidate = np.array([True, False, True, True, False, False, True])
    np.testing.assert_array_equal(confirmed(candidate, "two_of_three"), [False, False, True, True, False, False, False])


def test_alert_code_keeps_unscored_points_missing():
    code = alert_code(np.array([.0001, .5, np.nan]), {"alpha": .001, "confirmation": "direct"})
    assert code[0] == 0 and code[1] == 1 and np.isnan(code[2])


# ---------------------------------------------------------------- metrics

def test_no_point_adjustment_and_missed_points_counted():
    p = np.ones((10, 1, 1)); p[5] = .00001
    labels = np.zeros_like(p, dtype=bool); labels[3:8] = True
    metrics = summarize_counts(point_counts(p, labels, np.ones_like(labels), .001))
    assert metrics["tp"] == 1 and metrics["recall_end_to_end"] == .2
    events = event_metrics([{"start": 3, "end": 8, "sites": [0], "variables": [0]}], p, labels, .001)
    assert events[0]["delay_steps"] == 2 and events[0]["delay_minutes"] == 60


def test_false_episode_denominator_and_coverage():
    p = np.ones((48, 1, 1)); p[3:6] = .00001; p[12] = .00001; p[20:30] = np.nan
    result = summarize_counts(point_counts(p, np.zeros_like(p, dtype=bool), np.ones((48, 1, 1), dtype=bool), .001))
    assert result["false_episodes"] == 2 and result["false_episodes_per_stream_day"] == 2 and result["coverage"] == 38/48
