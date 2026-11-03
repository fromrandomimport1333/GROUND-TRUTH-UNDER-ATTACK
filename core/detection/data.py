"""Data: WegenerNet NetCDF -> compact cache of virtual streams, chronological partitions, replay episodes.

``extract`` converts the 24 monthly NetCDF files into a compact cache of selected grid cells
("virtual streams") with temperature (degC), relative humidity (%) and rain (mm/30 min) plus
their quality flags. ``load_partition`` reads one chronological split (train / calibration /
validation / test), verifying every hash. ``episodes`` cuts a partition into fixed 14-day
replay episodes. Configuration: the ``data`` section of ``config/detector.yaml``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from core.detection.utils import ROOT, VARIABLES, config, sha256, write_json


@dataclass
class StreamData:
    """Observation block: ``values``/``flags`` shaped [time, site, variable], interval ends (``times``), interval starts and site table."""
    times: np.ndarray
    starts: np.ndarray
    values: np.ndarray  # time, site, variable; original physical units
    flags: np.ndarray
    sites: pd.DataFrame

    def slice(self, start, stop):
        """Return the frames [start, stop) as a new StreamData (same sites)."""
        return StreamData(self.times[start:stop], self.starts[start:stop],
                          self.values[start:stop], self.flags[start:stop], self.sites)

    def with_values(self, values):
        """Return a copy with replaced values (e.g. an attacked version); flags and timestamps are unchanged."""
        if values.shape != self.values.shape:
            raise ValueError("Observable snapshot shape mismatch")
        return StreamData(self.times, self.starts, values, self.flags, self.sites)


def extract(cfg=None, force=False):
    """Extract the configured variables for a regular 1.5 km grid of cells into ``data/cache``.

    Validates units, the interval-end time convention, 30-minute continuity and grid
    consistency; selects sites with >= 95% clean training availability; writes one .npz
    per month, ``selection.parquet`` and a ``manifest.json`` with all hashes. An existing
    cache with the same configuration and unchanged sources is reused.
    """
    cfg = cfg or config("data")
    cache = ROOT / cfg["cache"]
    cache.mkdir(parents=True, exist_ok=True)
    manifest_path = cache / "manifest.json"
    if manifest_path.exists() and not force:
        manifest = json.loads(manifest_path.read_text())
        if manifest["configuration"] != cfg:
            raise ValueError("Extraction config changed; use --force to rebuild")
        for item in manifest["sources"]:
            p = ROOT / item["path"]
            if p.stat().st_size != item["size"] or p.stat().st_mtime_ns != item["mtime_ns"]:
                raise ValueError("Source changed; rebuild extraction")
        return manifest
    sources = sorted(ROOT.glob(cfg["source_glob"]))
    if not sources:
        raise FileNotFoundError("No WegenerNet NetCDF source files found")
    begin = time.perf_counter()
    records = []
    with xr.open_dataset(sources[0], engine="h5netcdf") as ds:
        # Regular sub-grid: every ``stride``-th cell (1.5 km) after a border ``margin``.
        y, x = np.meshgrid(np.arange(cfg["margin"], ds.sizes["Y"], cfg["stride"]),
                           np.arange(cfg["margin"], ds.sizes["X"], cfg["stride"]), indexing="ij")
        yi, xi = y.ravel(), x.ravel()
        sites = pd.DataFrame({"site_id": [f"grid_y{a:03d}_x{b:03d}" for a, b in zip(yi, xi)],
                              "y_index": yi, "x_index": xi,
                              "easting_m": ds.X.values[xi], "northing_m": ds.Y.values[yi]})
        grid_x, grid_y = ds.X.values.copy(), ds.Y.values.copy()
    training_good = np.zeros((len(sites), 3), dtype=np.int64)
    training_count = 0
    cache_files = []
    last_time = None
    for path in sources:
        print(f"Extracting {path.name}", flush=True)
        values, flags = [], []
        with xr.open_dataset(path, engine="h5netcdf", decode_cf=True, mask_and_scale=True) as ds:
            # Sanity checks: same grid, expected units, interval-end timestamps, 30-minute steps, no gaps.
            if not np.array_equal(ds.X.values, grid_x) or not np.array_equal(ds.Y.values, grid_y):
                raise ValueError("Grid changed between source files")
            if ds.T_z300m_HHM.attrs.get("units") != "K":
                raise ValueError("Unexpected temperature unit")
            if ds.RH_HHM.attrs.get("units") not in ("%", "percent") or ds.Precip_HHS.attrs.get("units") != "mm":
                raise ValueError("Unexpected humidity or precipitation unit")
            times = ds.time.values.astype("datetime64[ns]")
            bounds = ds.time_bnds.values.astype("datetime64[ns]")
            if bounds.shape != (len(times), 2) or not np.array_equal(bounds[:, 1], times):
                raise ValueError("Unexpected interval-end convention")
            if not np.all(bounds[:, 1] - bounds[:, 0] == np.timedelta64(30, "m")):
                raise ValueError("Unexpected interval duration")
            if not np.all(np.diff(times) == np.timedelta64(30, "m")):
                raise ValueError("Time gap or duplicate in archive")
            if last_time is not None and times[0] - last_time != np.timedelta64(30, "m"):
                raise ValueError("Gap between monthly files")
            last_time = times[-1]
            # Full compressed fields are read once per time block; paired NumPy
            # indexing then selects all cells without per-site decompression.
            for start in range(0, len(times), cfg["block_steps"]):
                v, q = [], []
                for variable, (source, flag, _) in cfg["variables"].items():
                    arr = ds[source].isel(time=slice(start, start + cfg["block_steps"])).values[:, yi, xi].astype("float32")
                    if variable == "temperature_c":
                        arr = arr - np.float32(273.15)
                    v.append(arr)
                    q.append(ds[flag].isel(time=slice(start, start + cfg["block_steps"])).values[:, yi, xi].astype("float32"))
                values.append(np.stack(v, axis=-1))
                flags.append(np.stack(q, axis=-1))
            v, q = np.concatenate(values), np.concatenate(flags)
            # Count clean training values per site/variable for the availability-based site selection.
            train = (bounds[:, 0] >= np.datetime64(cfg["split"]["train"][0])) & (bounds[:, 0] < np.datetime64(cfg["split"]["train"][1]))
            training_good += (np.isfinite(v[train]) & (q[train] == 0)).sum(axis=0)
            training_count += int(train.sum())
            out = cache / (path.stem + ".npz")
            np.savez_compressed(out, times=times, starts=bounds[:, 0], values=v, flags=q)
            cache_files.append(out)
        records.append({"path": str(path.relative_to(ROOT)), "sha256": sha256(path),
                        "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns})
    if training_count == 0:
        raise ValueError("Training coverage missing")
    good_fraction = training_good / training_count
    # Keep sites whose three variables are all clean for >= 95% of training time.
    selected = np.all(good_fraction >= cfg["min_training_availability"], axis=1)
    if selected.sum() < 25:
        raise ValueError("Insufficient training-quality locations for spatial rings")
    sites["training_quality_fraction_min"] = good_fraction.min(axis=1)
    sites["selected"] = selected
    sites.to_parquet(cache / "selection.parquet", index=False)
    manifest = {"schema": 1, "configuration": cfg, "sources": records,
                "variables": list(VARIABLES), "candidate_sites": len(sites), "selected_sites": int(selected.sum()),
                "training_steps": training_count, "selection_sha256": sha256(cache / "selection.parquet"),
                "cache_files": [{"name": p.name, "sha256": sha256(p)} for p in cache_files],
                "interval_ownership": "interval_start", "spacing_m": cfg["stride"] * 100,
                "dataset": "WegenerNet Level 2 v8.0 2025.1; doi:10.25364/WEGC/WPS8.0-2025.1; CC BY 4.0",
                "extraction_seconds": time.perf_counter() - begin}
    write_json(manifest_path, manifest)
    return manifest


def load_partition(partition, cfg=None):
    """Load one named split (by interval start) of the selected sites from the verified cache; months outside the split are never opened."""
    cfg = cfg or config("data")
    if partition not in cfg["split"]:
        raise ValueError(f"Unknown partition {partition}")
    cache = ROOT / cfg["cache"]
    manifest = json.loads((cache / "manifest.json").read_text())
    # Refuse a cache built with another configuration or a modified selection.
    if cfg != manifest["configuration"]:
        raise ValueError("Cache configuration mismatch")
    if sha256(cache / "selection.parquet") != manifest["selection_sha256"]:
        raise ValueError("Selection manifest changed")
    all_sites = pd.read_parquet(cache / "selection.parquet")
    selected = all_sites.selected.to_numpy()
    sites = all_sites.loc[selected].reset_index(drop=True)
    lo, hi = map(np.datetime64, cfg["split"][partition])
    chunks = []
    for item in manifest["cache_files"]:
        # Filter by source month BEFORE opening values: training never reads 2024 targets.
        month = np.datetime64(item["name"].removesuffix(".npz").split("_")[-1], "M")
        if month >= hi or month + np.timedelta64(1, "M") <= lo:
            continue
        path = cache / item["name"]
        if sha256(path) != item["sha256"]:
            raise ValueError("Extracted data hash mismatch")
        with np.load(path) as z:
            mask = (z["starts"] >= lo) & (z["starts"] < hi)
            chunks.append((z["times"][mask], z["starts"][mask], z["values"][mask][:, selected], z["flags"][mask][:, selected]))
    if not chunks:
        raise ValueError("No cached data in requested partition")
    return StreamData(*(np.concatenate([c[i] for c in chunks]) for i in range(4)), sites)


def episodes(data, episode_steps=672, warmup_steps=48):
    """Yield (episode_id, StreamData) blocks of ``episode_steps`` frames (672 = 14 days).

    Detector state resets at every episode; a final partial block must exceed the warm-up.
    """
    for start in range(0, len(data.times), episode_steps):
        stop = min(start + episode_steps, len(data.times))
        if stop - start > warmup_steps:
            yield start // episode_steps, data.slice(start, stop)
