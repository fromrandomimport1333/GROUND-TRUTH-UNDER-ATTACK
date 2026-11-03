import numpy as np
import pandas as pd

from data_prep.build import diurnal_climatology
from data_prep.stations import (
    distance_km,
    nearest_neighbors,
    sigma_ref,
    station_locations,
)


def test_diurnal_climatology_has_288_bins_normalised():
    idx = pd.date_range("2023-01-01", "2023-01-31", freq="5min", tz="UTC")
    rng = np.random.default_rng(0)
    values = pd.DataFrame({"1": rng.normal(size=len(idx))}, index=idx)
    shape = diurnal_climatology(values)
    assert shape.shape[0] == 288
    assert 0.0 <= shape["1"].min() and shape["1"].max() <= 1.0


def _geojson(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _feat(sid, lon, lat, alt, dt):
    return {
        "type": "Feature",
        "id": sid,
        "geometry": {"type": "Point", "coordinates": [lon, lat, alt]},
        "properties": {"datetime": dt, "label": str(sid)},
    }


def test_location_picked_by_validity_interval():
    gj = _geojson(
        _feat(83, 15.0, 47.0, 300.0, "2007-01-01T00:00:00Z/2023-03-08T00:00:00Z"),
        _feat(83, 15.1, 47.1, 310.0, "2023-03-08T00:00:00Z/2099-12-31T00:00:00Z"),
    )
    meta = station_locations(gj, pd.Timestamp("2024-01-01T00:00Z"))
    assert meta.loc["83", "lon"] == 15.1
    assert meta.loc["83", "alt_m"] == 310.0


def test_nearest_neighbors_excludes_self_and_is_sorted():
    meta = pd.DataFrame(
        {"lon": [15.0, 15.01, 15.02, 15.03], "lat": [47.0, 47.0, 47.0, 47.0]},
        index=pd.Index(["1", "2", "3", "4"], name="station"),
    )
    nn = nearest_neighbors(distance_km(meta), k=2)
    assert nn["1"] == ["2", "3"]


def test_sigma_ref_of_constant_offset_is_zero():
    idx = pd.date_range("2023-01-01", periods=50, freq="5min", tz="UTC")
    base = pd.Series(np.arange(50.0), index=idx)
    values = pd.DataFrame({"1": base + 2.0, "2": base, "3": base})
    sig = sigma_ref(values, {"1": ["2", "3"], "2": ["1", "3"], "3": ["1", "2"]})
    assert abs(sig["1"]) < 1e-6
