"""Latency and memory of the frozen Layer 3 detector on a laptop CPU.

    python -m eval.detector_overhead                    # writes reports/detector-overhead.json
    python -m eval.detector_overhead --ticks 100 --out local/overhead.json

What it measures (one process, CPU only, artifacts from ``artifacts/``):

* ``load``: time to verify the artifact hashes and load extractor, models and calibration.
* ``episode``: one 14-day replay episode (672 steps x 162 sites x 3 variables), the batch mode used for
  evaluation. Reported as total seconds and microseconds per scored reading.
* ``live_tick``: the online mode. Every 30 minutes the network delivers one new step; the detector scores it
  with the 48 steps (24 h) of causal history it needs. Reported as the latency from "new step available" to
  "162 x 3 p-values and alerts ready", median and p95 over ``--ticks`` ticks.
* ``peak_rss_mb``: peak resident memory of the process (``ru_maxrss``), after each phase.

The input is synthetic but has the exact production shape, timestamps and site table (diurnal temperature,
humidity anti-correlated with it, sparse rain, all quality flags 0). Latency and memory depend on the shape
of the arrays and the model, not on the values, so no data download is needed. Detection quality is measured
separately on real data (core/docs/README.md).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
from pathlib import Path

import numpy as np

from core.detection.data import StreamData
from core.detection.model import Detector
from core.detection.utils import config


def rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def synthetic_stream(sites, steps, seed=0, start="2024-03-01T00:30"):
    rng = np.random.default_rng(seed)
    n = len(sites)
    times = np.datetime64(start) + np.arange(steps) * np.timedelta64(30, "m")
    hours = (np.arange(steps) * 0.5) % 24
    diurnal = np.sin((hours - 9) / 24 * 2 * np.pi)[:, None]
    temp = 8 + 6 * diurnal + rng.normal(0, 0.3, (steps, n)) + rng.normal(0, 0.5, (1, n))
    hum = np.clip(75 - 15 * diurnal + rng.normal(0, 2, (steps, n)), 20, 100)
    rain = np.where(rng.random((steps, n)) < 0.08, rng.gamma(0.6, 0.6, (steps, n)), 0.0)
    values = np.stack([temp, hum, rain], axis=-1).astype("float32")
    return StreamData(times, times - np.timedelta64(30, "m"), values, np.zeros_like(values), sites)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ticks", type=int, default=50, help="live ticks to time")
    p.add_argument("--out", type=Path, default=Path("reports/detector-overhead.json"))
    args = p.parse_args(argv)
    warmup = config("features")["warmup_steps"]
    episode_steps = config("features")["episode_steps"]
    result = {"python": platform.python_version(), "platform": platform.platform(),
              "cpu_count": os.cpu_count(), "rss_after_imports_mb": rss_mb()}

    t0 = time.perf_counter()
    detector = Detector()
    result["load_s"] = round(time.perf_counter() - t0, 3)
    result["rss_after_load_mb"] = rss_mb()
    sites = detector.extractor.sites
    n_sites, n_vars = len(sites), 3

    episode = synthetic_stream(sites, episode_steps)
    detector(episode)  # warm caches once
    t0 = time.perf_counter()
    pvals, _ = detector(episode)
    dt = time.perf_counter() - t0
    scored = int(np.isfinite(pvals).sum())
    result["episode"] = {"steps": episode_steps, "sites": n_sites, "variables": n_vars, "seconds": round(dt, 3),
                         "scored_readings": scored, "us_per_reading": round(dt / max(scored, 1) * 1e6, 2)}
    result["rss_after_episode_mb"] = rss_mb()

    stream = synthetic_stream(sites, warmup + args.ticks, seed=1)
    lat = []
    for k in range(args.ticks):
        window = stream.slice(k, k + warmup + 1)  # 24 h history + the new step
        t0 = time.perf_counter()
        p_tick, a_tick = detector(window)
        lat.append(time.perf_counter() - t0)
        assert np.isfinite(p_tick[-1]).any()
    lat_ms = np.asarray(lat) * 1000
    result["live_tick"] = {"ticks": args.ticks, "history_steps": warmup, "readings_per_tick": n_sites * n_vars,
                           "median_ms": round(float(np.median(lat_ms)), 1),
                           "p95_ms": round(float(np.percentile(lat_ms, 95)), 1),
                           "max_ms": round(float(lat_ms.max()), 1),
                           "budget_ms": 30 * 60 * 1000,
                           "median_ms_per_reading": round(float(np.median(lat_ms)) / (n_sites * n_vars), 3)}
    result["peak_rss_mb"] = rss_mb()
    result["artifacts_mb"] = round(sum(f.stat().st_size for f in Path("artifacts").iterdir()) / 1e6, 1)
    result["input"] = "synthetic, production shape (see module docstring)"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
