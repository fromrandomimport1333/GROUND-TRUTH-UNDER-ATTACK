import io

import numpy as np
import pandas as pd

from data_prep.wegenernet import VARIABLES, read_station_csv, to_wide

SAMPLE = (
    '"Station","Time [YYYY-MM-DD HH:MM:SS]","Relative humidity [%]","Relative humidity DP-Flag []",'
    '"Precipitation [mm]","Precipitation DP-Flag []","Air temperature [degC]","Air temperature DP-Flag []"\n'
    "1,2023-01-01 00:00:00,100.0,0,0.0,2,-0.06,0\n"
    "1,2023-01-01 00:05:00,99.5,0,0.1,0,-0.21,1\n"
)


def test_read_station_csv_maps_columns_to_short_variable_names():
    df = read_station_csv(io.StringIO(SAMPLE))
    assert set(VARIABLES) == {"air_temperature", "relative_humidity", "precipitation"}
    assert df.loc[pd.Timestamp("2023-01-01 00:05", tz="UTC"), "air_temperature"] == np.float32(-0.21)
    assert df.loc[pd.Timestamp("2023-01-01 00:00", tz="UTC"), "precipitation_flag"] == 2
    assert df.index.tz is not None


def test_to_wide_builds_time_by_station_table_with_nan_for_missing_station():
    a = read_station_csv(io.StringIO(SAMPLE))
    b = a.iloc[:1]  # station 2 only has the first timestamp
    values, flags = to_wide({1: a, 2: b}, "air_temperature")
    assert list(values.columns) == [1, 2]
    assert values.dtypes.unique().tolist() == [np.float32]
    assert np.isnan(values.loc[pd.Timestamp("2023-01-01 00:05", tz="UTC"), 2])
    assert flags.loc[pd.Timestamp("2023-01-01 00:05", tz="UTC"), 1] == 1
