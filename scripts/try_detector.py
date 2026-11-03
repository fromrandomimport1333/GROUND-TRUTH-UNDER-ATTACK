"""Plant one attack in real 2024 data and see what the trained Layer 3 detector says.

    python -m scripts.try_detector                                   # default: +3 degC bias, 12 h
    python -m scripts.try_detector --family drift --amplitude 4 --duration 48
    python -m scripts.try_detector --family freeze --variable relative_humidity_pct --site 40
    python -m scripts.try_detector --episode 10 --start 200 --csv local/try.csv

Needs the trained model in ``artifacts/`` and the extracted cache in ``data/cache`` (see
docs/jury-guide.md). The attack is applied to the *observed* values of one 14-day episode of the 2024
test year; the clean values are scored too, so every alert outside the attack window is a false alarm.
Output: a summary on screen and, with ``--csv``, one row per (time, site, variable) with the p-value,
the alert and whether the point was attacked.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from core.detection.attacks import inject
from core.detection.data import episodes, load_partition
from core.detection.model import Detector
from core.detection.utils import VARIABLES, config

FAMILIES = ("bias", "drift", "spike", "freeze", "noise", "flatten", "rain_scale", "invalid_range")
UNITS = {"temperature_c": "degC", "relative_humidity_pct": "%", "precip_mm_30min": "mm/30 min"}


def when(t):
    return pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--family", choices=FAMILIES, default="bias")
    p.add_argument("--variable", choices=VARIABLES, default="temperature_c")
    p.add_argument("--amplitude", type=float, default=3.0,
                   help="size in the variable's unit (bias, drift, spike, invalid_range), noise std, or rain factor "
                        "(rain_scale); ignored by freeze and flatten")
    p.add_argument("--site", type=int, default=10, help="site index 0..161 (see data/cache/selection.parquet)")
    p.add_argument("--episode", type=int, default=5, help="14-day episode of 2024, 0..26")
    p.add_argument("--start", type=int, default=120, help="attack start, in 30-min steps inside the episode (>= 48)")
    p.add_argument("--duration", type=int, default=24, help="attack length in 30-min steps (24 = 12 h)")
    p.add_argument("--csv", type=Path, help="write every scored point to this CSV")
    return p.parse_args(argv)


def main(argv=None):
    args = parse(argv)
    warmup = config("features")["warmup_steps"]
    if args.start < warmup:
        raise SystemExit(f"--start must be >= {warmup}: the first {warmup} steps (24 h) of an episode are warm-up")
    detector = Detector()
    chosen = next((e for eid, e in episodes(load_partition("test")) if eid == args.episode), None)
    if chosen is None:
        raise SystemExit("--episode must be between 0 and 26")
    end = args.start + args.duration
    if end > len(chosen.times):
        raise SystemExit(f"attack must end inside the episode ({len(chosen.times)} steps)")
    v = VARIABLES.index(args.variable)
    spec = {"family": args.family, "start": args.start, "end": end, "sites": [args.site], "variables": [v],
            "amplitude": args.amplitude, "factor": args.amplitude}   # rain_scale reads "factor"
    injection = inject(chosen.values, [spec], seed=0)
    site_id = chosen.sites.site_id[args.site]

    p_clean, alerts_clean = detector(chosen)
    p, alerts = detector(chosen.with_values(injection.observed))
    attacked = injection.changed[:, args.site, v]
    window = np.zeros(len(chosen.times), dtype=bool)
    window[args.start:end] = True
    hits = alerts[:, args.site, v] & window
    first = int(np.flatnonzero(hits)[0]) if hits.any() else None

    print(f"\nEpisode {args.episode}: {when(chosen.times[0])} -> {when(chosen.times[-1])} UTC, {len(chosen.sites)} sites, 3 variables")
    size = ("" if args.family in ("freeze", "flatten") else
            f" x{args.amplitude:g}" if args.family == "rain_scale" else f" {args.amplitude:g} {UNITS[args.variable]}")
    print(f"Attack: {args.family}{size} on {args.variable} at site {site_id} "
          f"(index {args.site}), steps {args.start}-{end} = {when(chosen.times[args.start])} -> {when(chosen.times[end - 1])} UTC")
    print(f"Points actually changed by the attack: {int(attacked.sum())} of {args.duration}")
    if not attacked.any():
        print("  -> the attack changed nothing here (e.g. scaling zero rain, or missing values); "
              "pick another --start/--episode/--site")
    print("\nAttacked stream (this site, this variable):")
    print(f"  alerts during the attack : {int(hits.sum())} of {args.duration} steps "
          f"({100 * hits.sum() / args.duration:.0f} %)")
    print(f"  detected                 : {'YES' if first is not None else 'NO'}"
          + (f", first alert after {(first - args.start) * 30} min (step {first})" if first is not None else ""))
    print(f"  lowest p-value in window : {np.nanmin(p[args.start:end, args.site, v]):.2e} "
          f"(alert when p <= {detector.policy[v]['alpha']:g}, rule '{detector.policy[v]['confirmation']}')")
    outside = alerts.copy()
    outside[args.start:end, args.site, v] = False
    print("\nEverywhere else (false alarms):")
    print(f"  with the attack present  : {int(outside.sum())} alerts on "
          f"{int(np.isfinite(p).sum()) - int(np.isfinite(p[args.start:end, args.site, v]).sum())} scored points")
    print(f"  same episode, no attack  : {int(alerts_clean.sum())} alerts on {int(np.isfinite(p_clean).sum())} scored points")

    if args.csv:
        t, s, k = np.indices(p.shape)
        frame = pd.DataFrame({"time": chosen.times[t.ravel()], "site_id": chosen.sites.site_id.to_numpy()[s.ravel()],
                              "variable": np.asarray(VARIABLES)[k.ravel()], "observed": injection.observed.ravel(),
                              "clean": chosen.values.ravel(), "attacked": injection.changed.ravel(),
                              "p_value": p.ravel(), "alert": alerts.ravel()})
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        frame.dropna(subset=["p_value"]).to_csv(args.csv, index=False)
        print(f"\nWrote {args.csv} (one row per scored time x site x variable)")


if __name__ == "__main__":
    main()
