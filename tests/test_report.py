"""Report figures: the plotting helpers write non-empty PNGs for every family."""

import numpy as np
import pandas as pd

from eval.report import gallery_figure, sensitivity_figure


def test_sensitivity_figure_written(tmp_path):
    events = pd.DataFrame({
        "family": ["offset"] * 5 + ["drift"] * 5,
        "size": [0.5, 1, 2, 3, 5] * 2,
        "detected": [False, False, True, True, True, False, True, True, True, True],
    })
    out = tmp_path / "sensitivity.png"
    sensitivity_figure(events, out)
    assert out.exists() and out.stat().st_size > 0


def test_gallery_figure_covers_each_family(tmp_path):
    idx = pd.date_range("2024-07-01", periods=288 * 3, freq="5min", tz="UTC", name="time")
    clean = pd.DataFrame({"77": 10 + np.sin(np.arange(len(idx)) / 46).astype("float32")}, index=idx)
    faulted = clean.copy()
    faulted.iloc[288:576] += 2.0
    labels = pd.DataFrame({
        "scenario_id": ["showcase-offset-ai-0000"],
        "family": ["offset"], "variable": ["air_temperature"], "station": ["77"],
        "start": [idx[288]], "end": [idx[576]],
    })
    out = tmp_path / "gallery.png"
    gallery_figure({"air_temperature": clean}, {"air_temperature": faulted}, labels, out)
    assert out.exists() and out.stat().st_size > 0
