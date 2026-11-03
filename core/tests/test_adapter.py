"""Layer 2 FeatureWindow / compact CSV -> detector StreamData adapter."""
import csv
import gzip
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core.contracts import FeatureGroup, FeatureValue, FeatureWindow, ProcessedReading, SignedReading, VerifiedReading
from core.detection import adapter
from core.detection.data import StreamData
from core.detection.utils import VARIABLES

QUANTITIES = [adapter.DEFAULT_QUANTITIES[v] for v in VARIABLES]
UNITS = ["K", "%", "mm"]
T0 = datetime(2023, 11, 1, 0, 30, tzinfo=timezone.utc)


def grid_sites(n=3):
    """Detector-style sites on a 15-cell stride (grid_y008_x008, ...)."""
    ids = [(8 + 15*i, 8 + 15*j) for i in range(n) for j in range(n)]
    return pd.DataFrame({"site_id": [f"grid_y{y:03d}_x{x:03d}" for y, x in ids],
                         "easting_m": [x*100. for _, x in ids], "northing_m": [y*100. for y, _ in ids]})


def reading(cell, quantity, unit, value, when, status="valid", seq=0):
    raw = SignedReading(schema_version="1.0", sensor_id=cell, key_id="k", stream_id=quantity,
                        timestamp=when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), sequence=seq, quantity=quantity,
                        value=None if value is None else float(value), unit=unit, prev_hash=b"\0"*32, signature=b"\0"*64)
    verdict = VerifiedReading(schema_version="1.0", reading=raw, status=status,
                              signature_verified=status != "bad_signature", checked_at=when)
    ok = verdict.analysis_eligible
    return ProcessedReading(provenance=verdict, normalized_value=float(value) if ok else None,
                            normalized_unit=unit if ok else None)


def window(cell, when, values, flagged=(0., 0., 0.), status="valid", wid=None, neighbor=None):
    items = [reading(cell, q, u, v, when, status) for q, u, v in zip(QUANTITIES, UNITS, values)]
    if neighbor:
        items += [reading(neighbor, q, u, 999.0, when) for q, u in zip(QUANTITIES, UNITS)]
    tf = [FeatureValue(name=f"{q}__flagged_observation_count", sensor_id=cell, value=f) for q, f in zip(QUANTITIES, flagged)]
    tf += [FeatureValue(name=f"{q}__latest", sensor_id=cell, value=float(v)) for q, v in zip(QUANTITIES, values)]
    return FeatureWindow(schema_version="1.0", window_id=wid or f"w_{cell}_{when:%Y%m%d%H%M}", start=when,
                         end=when + timedelta(minutes=30), as_of=when + timedelta(minutes=30), readings=tuple(items),
                         temporal_features=FeatureGroup(availability="available", values=tuple(tf)))


def l2_cell(site_id):
    y, x = adapter.grid_index(site_id)
    return f"wn_grid_y{y}_x{x}"


def test_windows_round_trip_units_order_and_time_convention():
    sites = grid_sites()
    rng = np.random.default_rng(1)
    truth = np.stack([rng.normal(5, 3, (4, 9)), rng.uniform(40, 90, (4, 9)), rng.uniform(0, 2, (4, 9))], axis=-1)
    windows = [window(l2_cell(s), T0 + timedelta(minutes=30*t), [truth[t, i, 0] + 273.15, truth[t, i, 1], truth[t, i, 2]],
                      neighbor=l2_cell(sites.site_id[(i+1) % 9]))
               for t in range(4) for i, s in reversed(list(enumerate(sites.site_id)))]   # shuffled site order
    data, coverage = adapter.from_windows(windows, sites)
    assert isinstance(data, StreamData) and data.values.shape == (4, 9, 3)
    np.testing.assert_allclose(data.values, truth, atol=1e-4)            # K -> degC, detector site order
    assert np.all(data.flags == 0)                                       # neighbour readings ignored
    assert data.times[0] == np.datetime64("2023-11-01T00:30")             # window.start = interval-end label
    assert data.starts[0] == np.datetime64("2023-11-01T00:00")
    assert coverage["covered_sites"] == 9 and coverage["input_cells_not_in_detector_grid"] == []


def test_rejected_and_flagged_readings_are_not_clean():
    sites = grid_sites(2)
    cell = l2_cell(sites.site_id[0])
    windows = [window(cell, T0, [280., 50., 0.]),
               window(cell, T0 + timedelta(minutes=30), [281., 50., 0.], status="bad_signature"),
               window(cell, T0 + timedelta(minutes=60), [282., 50., 0.], flagged=(1., 0., None))]
    data, _ = adapter.from_windows(windows, sites)
    assert np.isnan(data.values[1, 0]).all() and np.isnan(data.flags[1, 0]).all()   # L1 rejection -> missing
    assert data.values[2, 0, 0] == pytest.approx(282 - 273.15)
    assert np.isnan(data.flags[2, 0, 0]) and data.flags[2, 0, 1] == 0 and np.isnan(data.flags[2, 0, 2])
    assert np.isnan(data.values[:, 1:]).all()                                         # uncovered sites


def test_duplicates_gaps_off_grid_and_variable_mismatch():
    sites = grid_sites(2)
    cell = l2_cell(sites.site_id[0])
    w = window(cell, T0, [280., 50., 0.])
    data, coverage = adapter.from_windows([w, w, window("wn_grid_y118_x60", T0, [280., 50., 0.])], sites)
    assert len(data.times) == 1 and coverage["input_cells_not_in_detector_grid"] == ["wn_grid_y118_x60"]
    assert any("T_z300m_HHM" in m for m in coverage["variable_mismatches"])
    with pytest.raises(ValueError, match="Two different windows"):
        adapter.from_windows([w, window(cell, T0, [281., 50., 0.], wid="other")], sites)
    with pytest.raises(ValueError, match="Non-contiguous"):
        adapter.from_windows([w, window(cell, T0 + timedelta(hours=2), [280., 50., 0.])], sites)
    with pytest.raises(ValueError, match="Only 0 detector sites"):
        adapter.from_windows([window("wn_grid_y118_x60", T0, [280., 50., 0.])], sites)
    assert adapter.check_compatibility({"temperature_c": "T_z300m_HHM"}) == []


def test_compact_csv_uses_exact_flag_percent(tmp_path):
    sites = grid_sites(2)
    cell = l2_cell(sites.site_id[0])
    path = tmp_path / "compact.csv.gz"
    fields = ["timestamp", "cell_id", "window_id"] + [f"{q}__{c}" for q in QUANTITIES for c in ("eligible", "latest", "raw_flag_percent")]
    rows = [(T0, "True", "283.15", "12.5"), (T0 + timedelta(minutes=30), "False", "", "0"),
            (T0 + timedelta(minutes=60), "True", "284.15", "101")]
    with gzip.open(path, "wt", newline="") as f:
        out = csv.DictWriter(f, fieldnames=fields)
        out.writeheader()
        for when, eligible, latest, flag in rows:
            row = {"timestamp": when.isoformat(), "cell_id": cell, "window_id": f"w{when:%H%M}"}
            for q in QUANTITIES:
                row.update({f"{q}__eligible": eligible, f"{q}__latest": latest, f"{q}__raw_flag_percent": flag})
            out.writerow(row)
    data, _ = adapter.from_compact_csv([path], sites)
    assert data.values[0, 0, 0] == pytest.approx(10.0) and data.flags[0, 0, 0] == 12.5
    assert np.isnan(data.values[1, 0]).all() and np.isnan(data.flags[1, 0]).all()
    assert np.isnan(data.flags[2, 0]).all()                                           # fill value 101 -> unknown


class StubDetector:
    """Minimal frozen-detector stand-in: p = 0.5 after warm-up, alert where temperature > 30 degC."""
    def __init__(self, extractor):
        self.extractor = extractor

    def __call__(self, episode):
        p = np.full(episode.values.shape, 0.5, dtype="float32")
        p[:48] = np.nan
        p[~np.isfinite(episode.values)] = np.nan
        alert = np.isfinite(p) & (np.nan_to_num(episode.values) > 30)
        alert[..., 1:] = False
        return p, alert


def test_layer3_sink_with_window_pipeline():
    from core.pipeline import WindowPipeline
    stub_sites = grid_sites(6)
    detector = StubDetector(type("Extractor", (), {"sites": stub_sites})())
    with pytest.warns(UserWarning, match="variable mismatch"):
        sink = adapter.Layer3Sink(detector)
    pipeline = WindowPipeline(engine=None, layer3=sink)
    cell = l2_cell(stub_sites.site_id[0])
    for t in range(60):
        w = window(cell, T0 + timedelta(minutes=30*t), [273.15 + (40 if t == 55 else 10), 50., 0.])
        pipeline.deliver(w)
        pipeline.deliver(w)                                   # at-least-once redelivery is dropped
    scores, coverage = sink.flush()
    assert coverage["ticks"] == 60 and coverage["covered_sites"] == 1
    assert len(scores) == 12 * 3                              # 60 ticks - 48 warm-up, three variables
    hit = scores[scores.alert]
    assert len(hit) == 1 and hit.iloc[0].variable == "temperature_c"
    assert hit.iloc[0].time == np.datetime64("2023-11-01T00:30") + np.timedelta64(55*30, "m")
    assert not sink.assembly.ticks                            # buffer cleared
