"""Report figures: a fault gallery and the sensitivity (recall-vs-size) curves.

Palette is a CVD-safe pair (blue clean / orange faulted) with thin marks and direct labels,
per the dataviz guidance. PNGs go to docs/figures/.

usage: uv run python -m eval.report --suite showcase --events results/baseline/dev/events.parquet
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CLEAN_C, FAULT_C = "#2563eb", "#e8590c"  # CVD-safe blue / orange
FIGS = Path("docs/figures")


def gallery_figure(clean: dict, faulted: dict, labels: pd.DataFrame, out: Path) -> None:
    """One panel per scenario: clean (blue) vs faulted (orange) with the window shaded."""
    rows = list(labels.itertuples())
    n = len(rows)
    cols = min(3, n)
    r = (n + cols - 1) // cols
    fig, axes = plt.subplots(r, cols, figsize=(4 * cols, 2.2 * r), squeeze=False)
    for ax in axes.flat:
        ax.set_visible(False)
    for k, row in enumerate(rows):
        ax = axes.flat[k]
        ax.set_visible(True)
        var, st = row.variable, str(row.station)
        start, end = pd.Timestamp(row.start), pd.Timestamp(row.end)
        win = slice(start - pd.Timedelta("12h"), end + pd.Timedelta("12h"))
        cser, fser = clean[var][st].loc[win], faulted[var][st].loc[win]
        ax.plot(cser.index, cser.to_numpy(), color=CLEAN_C, lw=1.0, label="clean")
        ax.plot(fser.index, fser.to_numpy(), color=FAULT_C, lw=1.2, label="faulted")
        ax.axvspan(start, end, color=FAULT_C, alpha=0.08)
        ax.set_title(row.family, fontsize=9)
        ax.tick_params(labelsize=6)
        ax.set_xticks([])
    axes.flat[0].legend(fontsize=7, frameon=False, loc="upper right")
    fig.suptitle("Fault gallery (showcase)", fontsize=11)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)


def sensitivity_figure(events: pd.DataFrame, out: Path) -> None:
    """Recall vs fault size, one thin line per family, directly labelled."""
    fig, ax = plt.subplots(figsize=(6, 4))
    cmap = plt.get_cmap("tab10")
    for i, (fam, g) in enumerate(events.groupby("family")):
        curve = g.groupby("size")["detected"].mean()
        if curve.empty:
            continue
        ax.plot(curve.index, curve.to_numpy(), marker="o", ms=4, lw=1.5, color=cmap(i % 10))
        ax.annotate(fam, (curve.index[-1], curve.iloc[-1]), fontsize=7,
                    xytext=(4, 0), textcoords="offset points", va="center")
    ax.axhline(0.9, color="#888", lw=0.8, ls="--")
    ax.set_xlabel("fault size (σ_ref units / factor)")
    ax.set_ylabel("detection recall")
    ax.set_ylim(-0.03, 1.05)
    ax.set_title("Detection sensitivity by family")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)


def _load(path: Path, var: str) -> pd.DataFrame:
    df = pd.read_parquet(path / f"{var}.parquet")
    df.columns = df.columns.astype(str)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="showcase")
    ap.add_argument("--clean-dir", type=Path, default=Path("data/clean"))
    ap.add_argument("--suites-dir", type=Path, default=Path("data/suites"))
    ap.add_argument("--events", type=Path, default=Path("results/baseline/dev/events.parquet"))
    ap.add_argument("--out", type=Path, default=FIGS)
    args = ap.parse_args()
    vars_ = ("air_temperature", "relative_humidity", "precipitation")
    clean = {v: _load(args.clean_dir, v) for v in vars_}
    faulted = {v: _load(args.suites_dir / args.suite, v) for v in vars_}
    labels = pd.read_parquet(args.suites_dir / args.suite / "labels.parquet")
    gallery_figure(clean, faulted, labels, args.out / "gallery.png")
    if args.events.exists():
        sensitivity_figure(pd.read_parquet(args.events), args.out / "sensitivity.png")
    print(f"wrote figures to {args.out}")


if __name__ == "__main__":
    main()
