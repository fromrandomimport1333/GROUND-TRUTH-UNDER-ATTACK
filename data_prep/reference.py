"""An external temperature reference for the cross-source (remote-sensing) detector.

A network-wide bias shifts every station together, so neighbour consistency is blind to it
(the whole network still agrees with itself).  Catching it needs a source that did NOT move:
ERA5-Land 2 m temperature over the Feldbach bounding box (hourly, averaged to a single area
series), or, when no CDS key is configured, the area-mean of an independent station cluster
(the dev pool is interspersed through the same 22x16 km area, so it tracks the real regional
weather while a held-out-pool attack leaves it untouched).

The network spans only ~1-4 ERA5 cells, so this reference catches network-wide and large-cluster
faults, not per-station ones -- which is exactly what the other detectors already cover.

usage: uv run python -m data_prep.reference        # writes data/clean/reference_t2m.parquet
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

CLEAN = Path("data/clean")
FBR_BBOX = (46.93, 15.76, 47.07, 16.06)  # S, W, N, E of the Feldbach Region


def align_to_grid(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Reindex an (hourly) reference onto the 5-min grid, forward-filling within each hour."""
    s = series.sort_index()
    out = s.reindex(s.index.union(index)).ffill().reindex(index)
    out.index = index
    return out


def complementary_reference(values: pd.DataFrame, reference_stations: list[str]) -> pd.Series:
    """Area-mean of an independent set of stations (not part of the attacked pool)."""
    cols = [s for s in reference_stations if s in values.columns]
    if not cols:
        raise ValueError("no reference stations present in the value table")
    return values[cols].mean(axis=1)


def load_era5_t2m(index: pd.DatetimeIndex, bbox=FBR_BBOX) -> pd.Series:  # pragma: no cover
    """ERA5-Land 2 m temperature area-mean, aligned to ``index``. Needs a CDS key + cdsapi.

    Set CDS_API_KEY (and optionally CDS_API_URL) in the environment. Returns degC.
    """
    import cdsapi  # imported lazily so the fallback path needs no dependency

    client = cdsapi.Client()
    years = sorted({index[0].year, index[-1].year})
    req = {
        "variable": "2m_temperature",
        "year": [str(y) for y in years],
        "month": [f"{m:02d}" for m in range(1, 13)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": [bbox[2], bbox[1], bbox[0], bbox[3]],  # N, W, S, E
        "data_format": "netcdf",
    }
    target = CLEAN / "era5_t2m_raw.nc"
    client.retrieve("reanalysis-era5-land", req, str(target))
    import xarray as xr

    ds = xr.open_dataset(target)
    area = ds["t2m"].mean(dim=[d for d in ds["t2m"].dims if d != "time"]) - 273.15
    series = area.to_series()
    series.index = pd.to_datetime(series.index, utc=True)
    return align_to_grid(series, index)


def build_reference(index: pd.DatetimeIndex, values: pd.DataFrame | None = None,
                    reference_stations: list[str] | None = None) -> pd.Series:
    """ERA5 when a CDS key is set, else the complementary-station area-mean (needs values)."""
    if os.environ.get("CDS_API_KEY"):
        return load_era5_t2m(index)
    if values is None or reference_stations is None:
        raise RuntimeError(
            "No CDS_API_KEY set and no fallback stations given. Set CDS_API_KEY to use ERA5, "
            "or pass values + reference_stations for the complementary-pool reference."
        )
    return align_to_grid(complementary_reference(values, reference_stations), index)


def main() -> None:  # pragma: no cover
    t = pd.read_parquet(CLEAN / "air_temperature.parquet")
    t.columns = t.columns.astype(str)
    idx = t.loc["2024"].index
    from attacks.placement import split_stations  # local import avoids a cycle at module load

    pools = split_stations(list(t.columns))
    ref = build_reference(idx, values=t.loc["2024"], reference_stations=pools["dev"])
    ref.to_frame("reference_t2m").to_parquet(CLEAN / "reference_t2m.parquet")
    print(f"reference_t2m.parquet: {len(ref)} rows, mean {ref.mean():.2f} degC")


if __name__ == "__main__":
    main()
