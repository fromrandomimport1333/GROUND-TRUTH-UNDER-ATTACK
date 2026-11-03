"""CLI: measure a detector's escape magnitude per family on real data.

usage: uv run python -m eval.escape --detector eval.run_baselines:detect
"""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path

import pandas as pd

from attacks.adaptive import build_adaptive_suite, escape_table
from attacks.schema import dump_suite, load_suite

CLEAN = Path("data/clean")
VARS = ("air_temperature", "relative_humidity", "precipitation")
# σ-unit / factor grid the escape search sweeps (covers the dev + heldout ranges)
DEFAULT_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]


def _load_detector(spec: str):
    module, func = spec.split(":")
    return getattr(importlib.import_module(module), func)


def _load_clean() -> dict[str, pd.DataFrame]:
    out = {}
    for v in VARS:
        df = pd.read_parquet(CLEAN / f"{v}.parquet")
        df.columns = df.columns.astype(str)
        out[v] = df
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detector", required=True, help="module:function returning bool alarm tables")
    ap.add_argument("--bases", type=Path, default=Path("data/suites/showcase/scenarios.yaml"))
    ap.add_argument("--out", type=Path, default=Path("results/escape"))
    ap.add_argument("--grace", default="1h")
    args = ap.parse_args()

    detect = _load_detector(args.detector)
    clean = _load_clean()
    sigma = pd.read_csv(CLEAN / "sigma_ref.csv", index_col=0)
    sigma.index = sigma.index.astype(str)
    bases = list(load_suite(args.bases).scenarios)

    args.out.mkdir(parents=True, exist_ok=True)
    table = escape_table(bases, clean, sigma, detect, grid=DEFAULT_GRID, grace=args.grace)
    table.to_parquet(args.out / "escape.parquet")  # saved before the suite, so a slow run is not lost
    suite = build_adaptive_suite(bases, clean, sigma, detect, grid=DEFAULT_GRID, grace=args.grace)
    dump_suite(suite, args.out / "adaptive_probe.yaml")
    print(table.to_string(index=False))
    print(f"\nwrote {args.out/'escape.parquet'} and {args.out/'adaptive_probe.yaml'}")


if __name__ == "__main__":
    main()
