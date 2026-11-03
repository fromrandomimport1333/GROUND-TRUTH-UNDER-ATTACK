"""Assemble the standard-scenario suites: place the grids, inject, write reproducible files.

Each suite lands in ``data/suites/<name>/`` as ``{var}.parquet`` (the faulted tables, full
2023-2024 index so detectors warm up on 2023), ``labels.parquet`` (the answer key),
``scenarios.yaml`` (the placed suite) and ``manifest.json`` (sha256 of inputs and outputs,
no wall-clock times, so a rebuild is byte-identical).

usage: uv run python -m attacks.build_suites [--only smoke,dev,heldout,clean2024,showcase]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from attacks.grid import expand, load_grid
from attacks.injector import LABEL_COLUMNS, inject
from attacks.placement import PlacementContext, place, split_stations
from attacks.schema import Scenario, Suite, dump_suite, load_suite
from data_prep.stations import distance_km

VARS = ("air_temperature", "relative_humidity", "precipitation")
CLEAN = Path("data/clean")
SUITES = Path("data/suites")
SCENARIOS = Path(__file__).resolve().parent / "scenarios"
GRID_FILES = {"smoke": "grid_smoke.yaml", "dev": "grid_dev.yaml", "heldout": "grid_heldout.yaml"}
ALL_SUITES = (*GRID_FILES, "clean2024", "showcase", "crosssource")
CROSSSOURCE_SEED = 20264


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    # pyarrow + zstd is deterministic for identical data; no timestamp metadata is written.
    df.to_parquet(path, engine="pyarrow", compression="zstd", index=True)


def _load_clean(clean_dir: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for v in VARS:
        df = pd.read_parquet(clean_dir / f"{v}.parquet")
        df.columns = df.columns.astype(str)
        out[v] = df
    return out


def _load_flags(clean_dir: Path) -> dict[str, pd.DataFrame]:
    out = {}
    for v in VARS:
        df = pd.read_parquet(clean_dir / f"{v}_flag.parquet")
        df.columns = df.columns.astype(str)
        out[v] = df
    return out


def _sigma(clean_dir: Path) -> pd.DataFrame:
    s = pd.read_csv(clean_dir / "sigma_ref.csv", index_col=0)
    s.index = s.index.astype(str)
    return s


def _context(clean, flags, clean_dir: Path, cfg) -> PlacementContext:
    meta = pd.read_csv(clean_dir / "stations.csv", index_col=0)
    meta.index = meta.index.astype(str)
    meta = meta.loc[meta.index.isin(clean["air_temperature"].columns)]
    pools = split_stations(list(clean["air_temperature"].columns))
    if getattr(cfg, "max_stations", None):
        pool_name = cfg.station_pool if cfg.station_pool != "all" else cfg.split
        pools[pool_name] = pools[pool_name][: cfg.max_stations]
    index2024 = clean["air_temperature"].loc["2024"].index
    pool = None if cfg.station_pool == "all" else cfg.station_pool
    months = tuple(cfg.months) if cfg.months else None
    return PlacementContext(
        flags=flags, values=clean, dist=distance_km(meta), pools=pools,
        index=index2024, split=cfg.split, pool=pool, months=months,
    )


def _place_grid(name: str, clean, flags, clean_dir: Path) -> tuple[Suite, Path]:
    grid_path = SCENARIOS / GRID_FILES[name]
    cfg = load_grid(grid_path)
    specs = expand(cfg)
    ctx = _context(clean, flags, clean_dir, cfg)
    placed = place(specs, ctx, np.random.default_rng([cfg.seed, 0]))
    return Suite(name=name, seed=cfg.seed, scenarios=tuple(placed)), grid_path


def _crosssource_suite(clean) -> Suite:
    """network_bias over the whole held-out pool: every station shifted together.

    Neighbour consistency is blind to this (all stations still agree); only the cross-source
    reference catches it. Built directly (not via the grid) because the group is the whole pool.
    """
    from datetime import datetime, timedelta, timezone

    # The whole network is shifted together: a held-out-only shift would still disagree with
    # the unshifted stations among each station's neighbours, so neighbour consistency would
    # catch it. Shifting every station is what truly defeats neighbour checks.
    stations = tuple(clean["air_temperature"].columns)
    windows = [  # (k, start, hours) on vetted mid-summer clean windows
        (2.0, datetime(2024, 7, 5, 0, 0, tzinfo=timezone.utc), 24),
        (3.0, datetime(2024, 7, 12, 0, 0, tzinfo=timezone.utc), 72),
    ]
    scenarios = tuple(
        Scenario(
            id=f"crosssource-network_bias-ai-{i:04d}",
            family="network_bias",
            variable="air_temperature",
            stations=stations,
            start=start,
            end=start + timedelta(hours=hours),
            params={"k": k},
            split="heldout",
        )
        for i, (k, start, hours) in enumerate(windows)
    )
    return Suite(name="crosssource", seed=CROSSSOURCE_SEED, scenarios=scenarios)


def _empty_labels() -> pd.DataFrame:
    frame = pd.DataFrame({c: pd.Series(dtype="object") for c in LABEL_COLUMNS})
    for c in ("start", "end", "onset"):
        frame[c] = pd.to_datetime(frame[c], utc=True)
    for c in ("size", "size_phys", "duration_h"):
        frame[c] = frame[c].astype("float64")
    frame["n_stations"] = frame["n_stations"].astype("int64")
    return frame


def build_suite(name: str, *, clean_dir: Path = CLEAN, out_dir: Path = SUITES) -> dict:
    """Build one suite into ``out_dir/<name>/`` and return its manifest dict."""
    clean = _load_clean(clean_dir)
    dest = Path(out_dir) / name
    dest.mkdir(parents=True, exist_ok=True)
    inputs: dict[str, str] = {v: _sha(clean_dir / f"{v}.parquet") for v in VARS}

    if name == "clean2024":
        faulted, labels, config_src = {v: clean[v] for v in VARS}, _empty_labels(), None
        scenarios = None
    elif name == "showcase":
        suite = load_suite(SCENARIOS / "showcase.yaml")
        faulted, labels = inject(suite, clean, _sigma(clean_dir))
        config_src, scenarios = SCENARIOS / "showcase.yaml", suite
    elif name == "crosssource":
        suite = _crosssource_suite(clean)
        faulted, labels = inject(suite, clean, _sigma(clean_dir))
        config_src, scenarios = None, suite
    else:
        flags = _load_flags(clean_dir)
        suite, config_src = _place_grid(name, clean, flags, clean_dir)
        faulted, labels = inject(suite, clean, _sigma(clean_dir))
        scenarios = suite

    for v in VARS:
        _write_parquet(faulted[v], dest / f"{v}.parquet")
    _write_parquet(labels, dest / "labels.parquet")
    if scenarios is not None:
        dump_suite(scenarios, dest / "scenarios.yaml")

    outputs = {p.name: _sha(p) for p in sorted(dest.iterdir()) if p.name != "manifest.json"}
    manifest = {
        "suite": name,
        "seed": int(scenarios.seed) if scenarios is not None else None,
        "n_scenarios": 0 if scenarios is None else len(scenarios.scenarios),
        "config_sha256": _sha(config_src) if config_src else None,
        "inputs": inputs,
        "outputs": outputs,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated subset of " + ",".join(ALL_SUITES))
    ap.add_argument("--clean-dir", type=Path, default=CLEAN)
    ap.add_argument("--out-dir", type=Path, default=SUITES)
    args = ap.parse_args()
    names = args.only.split(",") if args.only else ALL_SUITES
    for name in names:
        m = build_suite(name, clean_dir=args.clean_dir, out_dir=args.out_dir)
        print(f"{name}: {m['n_scenarios']} scenarios -> {args.out_dir / name}")


if __name__ == "__main__":
    main()
