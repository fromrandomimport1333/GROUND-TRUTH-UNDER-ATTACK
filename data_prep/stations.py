"""Station locations, nearest neighbours, and per-station sigma_ref."""

from math import cos, radians

import numpy as np
import pandas as pd

EXCLUDE = {"18"}  # 100 % interpolated (decommissioned)


def station_locations(geojson: dict, at: pd.Timestamp) -> pd.DataFrame:
    """One row per station whose validity interval contains `at`; index str, cols lon/lat/alt_m."""
    rows = {}
    for f in geojson["features"]:
        sid = str(f["id"])
        start, end = (pd.Timestamp(t) for t in f["properties"]["datetime"].split("/"))
        if start <= at < end:
            lon, lat, alt = f["geometry"]["coordinates"]
            rows[sid] = {"lon": lon, "lat": lat, "alt_m": alt}
    meta = pd.DataFrame(rows).T
    meta.index.name = "station"
    return meta.sort_index()


def distance_km(meta: pd.DataFrame) -> pd.DataFrame:
    """Equirectangular station x station distance in km."""
    lat0 = radians(float(meta["lat"].mean()))
    x = meta["lon"].to_numpy(float) * 111.320 * cos(lat0)
    y = meta["lat"].to_numpy(float) * 110.574
    d = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])
    return pd.DataFrame(d, index=meta.index, columns=meta.index)


def nearest_neighbors(dist: pd.DataFrame, k: int = 5) -> dict[str, list[str]]:
    """k nearest stations per station (self excluded, excludes EXCLUDE stations)."""
    cols = [c for c in dist.columns if c not in EXCLUDE]
    out = {}
    for s in dist.index:
        if s in EXCLUDE:
            continue
        others = dist.loc[s, cols].drop(s).sort_values()
        out[s] = others.index[:k].tolist()
    return out


def sigma_ref(values: pd.DataFrame, neighbors: dict[str, list[str]]) -> pd.Series:
    """1.4826 * MAD of (station - median of its neighbours), NaN-safe, per station."""
    out = {}
    for s, nbrs in neighbors.items():
        if s not in values.columns:
            continue
        nbrs = [n for n in nbrs if n in values.columns]
        ref = values[nbrs].median(axis=1)
        resid = (values[s] - ref).to_numpy()
        resid = resid[~np.isnan(resid)]
        if resid.size == 0:
            out[s] = np.nan
            continue
        out[s] = float(1.4826 * np.median(np.abs(resid - np.median(resid))))
    return pd.Series(out, name="sigma_ref")
