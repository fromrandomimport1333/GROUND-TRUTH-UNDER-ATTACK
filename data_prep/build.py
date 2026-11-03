"""Build clean 2023-2024 Feldbach tables from the downloaded WegenerNet zips.

usage: uv run python -m data_prep.build
"""

import json
import re
import zipfile
from pathlib import Path

import pandas as pd

from data_prep.stations import EXCLUDE, distance_km, nearest_neighbors, sigma_ref, station_locations
from data_prep.wegenernet import VARIABLES, read_station_csv, to_wide

RAW = Path("data/raw/wegenernet")
OUT = Path("data/clean")
START, END = pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2024-12-31 23:55", tz="UTC")
AT = pd.Timestamp("2024-01-01T00:00Z")  # reference instant for station locations
FELDBACH_MAX_ID = 499  # 5xx = Gesaeuse region, 1xxx/11xxx = 3D instruments
DIURNAL_VARS = ("air_temperature", "relative_humidity")


def load_stations() -> dict[int, pd.DataFrame]:
    parts: dict[int, list[pd.DataFrame]] = {}
    for zpath in sorted(RAW.glob("WN_L2_V8_*_BD_*.zip")):
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                sid = int(re.search(r"_St(\d+)_", name).group(1))
                if sid > FELDBACH_MAX_ID:
                    continue
                with z.open(name) as f:
                    parts.setdefault(sid, []).append(read_station_csv(f))
    out = {}
    for sid, frames in parts.items():
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        out[sid] = df.loc[START:END]
    return out


def diurnal_climatology(values: pd.DataFrame) -> pd.DataFrame:
    """Normalised 0..1 mean diurnal shape per station from 2023 (288 5-min bins)."""
    v = values.loc["2023"]
    tod = v.index.hour * 12 + v.index.minute // 5
    mean = v.groupby(tod).mean()
    span = mean.max() - mean.min()
    shape = (mean - mean.min()) / span.replace(0.0, 1.0)
    shape.index.name = "tod_bin"
    return shape


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    stations = load_stations()
    full_index = pd.date_range(START, END, freq="5min", name="time")
    clean = {}
    for var in VARIABLES:
        values, flags = to_wide(stations, var)
        values = values.reindex(full_index)
        flags = flags.reindex(full_index).fillna(-1).astype("int8")
        values.columns = values.columns.astype(str)
        flags.columns = flags.columns.astype(str)
        values = values.drop(columns=[c for c in EXCLUDE if c in values.columns])
        flags = flags.drop(columns=[c for c in EXCLUDE if c in flags.columns])
        values.to_parquet(OUT / f"{var}.parquet")
        flags.to_parquet(OUT / f"{var}_flag.parquet")
        clean[var] = values
        print(f"{var}: {values.shape[1]} stations, {values.shape[0]} times, "
              f"{values.isna().mean().mean():.2%} missing, {(flags > 0).mean().mean():.2%} gap-filled")

    geojson = json.loads((RAW / "stations.geojson").read_text())
    meta = station_locations(geojson, AT)
    meta = meta.loc[meta.index.isin(clean["air_temperature"].columns)]
    meta.to_csv(OUT / "stations.csv")
    dist = distance_km(meta)
    neighbors = nearest_neighbors(dist, k=5)
    nn_rows = [
        {"station": s, "rank": i + 1, "neighbor": n, "distance_km": round(float(dist.loc[s, n]), 4)}
        for s, nbrs in neighbors.items() for i, n in enumerate(nbrs)
    ]
    pd.DataFrame(nn_rows).to_csv(OUT / "neighbors.csv", index=False)

    sig = pd.DataFrame({var: sigma_ref(clean[var], neighbors) for var in DIURNAL_VARS})
    sig.index.name = "station"
    sig.to_csv(OUT / "sigma_ref.csv")
    for var in DIURNAL_VARS:
        diurnal_climatology(clean[var]).to_parquet(OUT / f"diurnal_{var}.parquet")
    print(f"stations.csv: {len(meta)} stations | sigma_ref median "
          f"T {sig['air_temperature'].median():.3f}, RH {sig['relative_humidity'].median():.3f}")


if __name__ == "__main__":
    main()
