"""Run the reference baselines over a suite and write boolean alarm tables.

    uv run python -m eval.run_baselines --suite <name> [--variables air_temperature,...]

Reads ``data/suites/<name>/{variable}.parquet`` (``clean`` / ``clean2024`` fall back to ``data/clean``
while that suite has not been built) and writes, one variable at a time,

    alarms/baseline_rules/<name>/{variable}.parquet       range / step / constancy
    alarms/baseline_neighbors/<name>/{variable}.parquet   5-NN disagreement
    alarms/baseline_combined/<name>/{variable}.parquet    OR of the two

Alarm tables are bool with the same index and columns as the suite's value tables, so
``uv run python -m eval.run --suite <name> --alarms alarms/baseline_combined/<name>`` scores them.

``detect`` (all three baselines OR-ed) follows the plan's detector contract
``detect(values: dict[str, DataFrame]) -> dict[str, DataFrame]``, e.g. for ``eval.escape``.
These are reference floors, not the team detector.
"""

from __future__ import annotations

import argparse
import gc
from pathlib import Path

import pandas as pd

from eval.baselines import neighbor_baseline, rule_baseline

ROOT = Path(__file__).resolve().parents[1]
VARIABLES = ("air_temperature", "relative_humidity", "precipitation")
RUNS = ("baseline_rules", "baseline_neighbors", "baseline_combined")
CLEAN_FALLBACK = ("clean", "clean2024")


def load_neighbors(path: Path) -> dict[str, list[str]]:
    """``neighbors.csv`` (station, rank, neighbor, distance_km) to {station: [neighbours by rank]}."""
    frame = pd.read_csv(path, dtype={"station": str, "neighbor": str}).sort_values(["station", "rank"])
    return {str(s): [str(n) for n in g["neighbor"]] for s, g in frame.groupby("station", sort=True)}


def load_sigma(path: Path) -> pd.DataFrame:
    """``sigma_ref.csv`` to a frame indexed by station (str), one column per variable."""
    frame = pd.read_csv(path, dtype={"station": str}).set_index("station")
    frame.index = frame.index.astype(str)
    return frame


def baseline_tables(
    values: pd.DataFrame, variable: str, neighbors: dict[str, list[str]], sigma: pd.DataFrame
) -> dict[str, pd.DataFrame]:
    """The three alarm tables for one variable, keyed by run name."""
    rules = rule_baseline(values, variable)
    scale = sigma[variable] if variable in sigma.columns else None
    near = neighbor_baseline(values, neighbors, scale, variable=variable)
    return {"baseline_rules": rules, "baseline_neighbors": near, "baseline_combined": rules | near}


def detect(values: dict[str, pd.DataFrame], clean_dir: Path = ROOT / "data" / "clean") -> dict[str, pd.DataFrame]:
    """Detector-contract wrapper: combined baseline alarms for each variable in ``values``."""
    neighbors, sigma = load_neighbors(clean_dir / "neighbors.csv"), load_sigma(clean_dir / "sigma_ref.csv")
    return {
        var: baseline_tables(table, var, neighbors, sigma)["baseline_combined"] for var, table in values.items()
    }


def source_dir(suite: str, suites_dir: Path, clean_dir: Path) -> Path:
    path = suites_dir / suite
    if path.is_dir():
        return path
    if suite in CLEAN_FALLBACK:
        return clean_dir
    raise SystemExit(f"{path} not found; build the suite first (or use --suite clean for data/clean)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suite", required=True, help="suite name, e.g. smoke, dev, heldout, clean2024")
    ap.add_argument("--variables", help="comma-separated subset (default: all that exist in the suite)")
    ap.add_argument("--suites-dir", type=Path, default=ROOT / "data" / "suites")
    ap.add_argument("--clean-dir", type=Path, default=ROOT / "data" / "clean", help="neighbors.csv, sigma_ref.csv")
    ap.add_argument("--alarms-dir", type=Path, default=ROOT / "alarms", help="root for baseline_*/<suite>/")
    args = ap.parse_args(argv)

    src = source_dir(args.suite, args.suites_dir, args.clean_dir)
    wanted = args.variables.split(",") if args.variables else list(VARIABLES)
    variables = [v for v in wanted if (src / f"{v}.parquet").exists()]
    if not variables:
        raise SystemExit(f"none of {wanted} found in {src}")
    neighbors = load_neighbors(args.clean_dir / "neighbors.csv")
    sigma = load_sigma(args.clean_dir / "sigma_ref.csv")

    for variable in variables:  # one variable at a time: tables are ~210k x 154
        values = pd.read_parquet(src / f"{variable}.parquet")
        tables = baseline_tables(values, variable, neighbors, sigma)
        for run, alarm in tables.items():
            out = args.alarms_dir / run / args.suite
            out.mkdir(parents=True, exist_ok=True)
            alarm.to_parquet(out / f"{variable}.parquet", compression="zstd")
        rate = {run: float(a.to_numpy().mean()) for run, a in tables.items()}
        print(f"{args.suite}/{variable}: alarm share " + ", ".join(f"{r.removeprefix('baseline_')}={v:.5f}" for r, v in rate.items()))
        del values, tables
        gc.collect()
    print(f"wrote {', '.join(f'{args.alarms_dir}/{r}/{args.suite}' for r in RUNS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
