"""Shared fixtures: a small synthetic 6x6-site dataset and a feature extractor fitted on it."""
import copy

import numpy as np
import pandas as pd
import pytest

from core.detection.data import StreamData
from core.detection.features import FeatureExtractor
from core.detection.utils import config


def synthetic(n=576, start="2023-01-01"):
    """Deterministic diurnal temperature/humidity and intermittent rain on a 6x6 grid (1.5 km spacing)."""
    rng = np.random.default_rng(20)
    y, x = np.meshgrid(np.arange(6), np.arange(6), indexing="ij")
    sites = pd.DataFrame({"site_id": [f"site_{i}" for i in range(36)], "easting_m": x.ravel()*1500., "northing_m": y.ravel()*1500.})
    starts = np.datetime64(start, "ns") + np.arange(n)*np.timedelta64(30, "m")
    phase = np.arange(n)[:, None]*2*np.pi/48
    offset = (x.ravel()+y.ravel())/20
    temperature = 15+4*np.sin(phase)+offset+rng.normal(0, .1, (n, 36))
    humidity = 60-15*np.sin(phase)+offset+rng.normal(0, .3, (n, 36))
    rain = np.maximum(0, rng.normal(0, .2, (n, 36)))
    rain[np.arange(n) % 48 < 35] = 0
    values = np.stack([temperature, humidity, rain], axis=-1)
    return StreamData(starts+np.timedelta64(30, "m"), starts, values, np.zeros_like(values), sites)


@pytest.fixture(scope="session")
def fitted_extractor():
    data = synthetic()
    return FeatureExtractor(config("features"), data.sites).fit(data)


@pytest.fixture
def extractor(fitted_extractor):
    return copy.deepcopy(fitted_extractor)


@pytest.fixture
def data():
    return synthetic(110, "2023-11-01")
