"""Load WegenerNet L2 v8 5-min station CSVs into compact time x station tables."""

import pandas as pd

# short name -> column name prefix used in the WegenerNet CSV header
VARIABLES = {
    "air_temperature": "Air temperature",
    "relative_humidity": "Relative humidity",
    "precipitation": "Precipitation",
}
UNITS = {"air_temperature": "degC", "relative_humidity": "%", "precipitation": "mm"}


def read_station_csv(src) -> pd.DataFrame:
    """One station file -> DataFrame indexed by UTC time with `<var>` and `<var>_flag` columns."""
    raw = pd.read_csv(src, encoding="latin-1")
    time = pd.to_datetime(raw["Time [YYYY-MM-DD HH:MM:SS]"], utc=True)
    out = pd.DataFrame(index=pd.DatetimeIndex(time, name="time"))
    for short, prefix in VARIABLES.items():
        value_col = next((c for c in raw.columns if c.startswith(prefix + " [")), None)
        if value_col is None:
            continue
        out[short] = raw[value_col].to_numpy("float32")
        out[short + "_flag"] = raw[prefix + " DP-Flag []"].to_numpy("int8")
    return out


def to_wide(stations: dict[int, pd.DataFrame], variable: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-station frames -> (values, DP flags), each time x station; missing -> NaN / -1."""
    have = {sid: df for sid, df in sorted(stations.items()) if variable in df.columns}
    values = pd.DataFrame({sid: df[variable] for sid, df in have.items()}).astype("float32")
    flags = pd.DataFrame({sid: df[variable + "_flag"] for sid, df in have.items()})
    flags = flags.reindex(values.index).fillna(-1).astype("int8")
    return values, flags
