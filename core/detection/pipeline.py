"""End-to-end pipeline of the Layer 3 detector (CPU only).

    python -m core.detection.pipeline                # run every missing stage
    python -m core.detection.pipeline --stage select # run one stage
    python -m core.detection.pipeline --force        # rebuild everything

Stages and their outputs (``artifacts/`` and ``outputs/`` are git-ignored):

| stage     | data used            | output                                                        |
|-----------|----------------------|---------------------------------------------------------------|
| extract   | NetCDF 2023-2024     | data/cache (compact virtual streams)                          |
| features  | Jan-Aug 2023         | artifacts/feature_extractor.joblib                            |
| train     | Jan-Aug 2023 + attacks | artifacts/extra_trees.joblib                                |
| calibrate | Sep-Oct 2023 nominal | artifacts/calibration.npz                                     |
| select    | Nov-Dec 2023 + attacks | artifacts/policy.json (frozen, with artifact hashes)        |
| evaluate  | 2024 + two attack seeds | outputs/metrics.json, variables.csv, families.csv, RESULTS.md |
"""
from __future__ import annotations

import argparse
import time
from collections import defaultdict

import joblib
import numpy as np
import pandas as pd

from core.detection.attacks import scenarios
from core.detection.data import episodes, extract, load_partition
from core.detection.features import FeatureExtractor
from core.detection.metrics import aggregate_counts, event_metrics, point_counts, summarize_counts
from core.detection.model import (Detector, alert_code, alerts, calibration_tails, p_values, score, train,
                            training_rows)
from core.detection.utils import ARTIFACTS, OUTPUTS, ROOT, VARIABLES, config, read_json, sha256, write_json

EXTRACTOR = ARTIFACTS / "feature_extractor.joblib"
MODELS = ARTIFACTS / "extra_trees.joblib"
CALIBRATION = ARTIFACTS / "calibration.npz"
POLICY = ARTIFACTS / "policy.json"
STAGES = {"extract": ROOT / config("data")["cache"] / "manifest.json", "features": EXTRACTOR, "train": MODELS,
          "calibrate": CALIBRATION, "select": POLICY, "evaluate": OUTPUTS / "metrics.json"}


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def stage_features():
    """Fit the causal feature extractor on nominal Jan-Aug 2023."""
    data = load_partition("train")
    FeatureExtractor(config("features"), data.sites).fit(data).save(EXTRACTOR)


def stage_train():
    """Sample nominal + attacked training rows and fit the three Extra Trees models."""
    extractor = FeatureExtractor.load(EXTRACTOR)
    rows = training_rows(extractor, load_partition("train"), log)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    joblib.dump(train(rows, log), MODELS, compress=3)


def stage_calibrate():
    """Nominal Sep-Oct 2023 score tails, one per variable."""
    tails = calibration_tails(joblib.load(MODELS), FeatureExtractor.load(EXTRACTOR), load_partition("calibration"), log)
    np.savez_compressed(CALIBRATION, **{str(v): t for v, t in enumerate(tails)})


def stage_select():
    """Choose alpha and confirmation per variable on Nov-Dec 2023, then freeze the policy.

    Each validation episode is scored nominal and attacked (seed ``validation_seed``). A setting
    is feasible if nominal false-alert episodes per stream-day stay within the budget and
    coverage is at least 95 %; the feasible setting with the best F1 (then precision) wins.
    """
    cfg = config("policy")
    extractor, models = FeatureExtractor.load(EXTRACTOR), joblib.load(MODELS)
    with np.load(CALIBRATION) as z:
        tails = [z[str(v)] for v in range(3)]
    replays = []
    for eid, episode in episodes(load_partition("validation")):
        log(f"Selection: episode {eid}")
        injection = scenarios(episode, extractor, "validation", eid, cfg["validation_seed"])
        for kind, observed, labels in (("attacked", injection.observed, injection.changed),
                                       ("nominal", episode.values, np.zeros(episode.values.shape, dtype=bool))):
            s, ctx = score(models, extractor, episode.with_values(observed))
            replays.append((kind, p_values(s, tails), labels, ctx["eligible"]))
    rows = []
    for v in range(3):
        for mode in cfg["confirmation"]:
            for alpha in cfg["alpha_grid"]:
                counts = defaultdict(list)
                for kind, p, labels, eligible in replays:
                    a = alert_code(p[..., v], {"alpha": alpha, "confirmation": mode})
                    counts[kind].append(point_counts(a[..., None], labels[..., v:v+1], eligible[..., v:v+1], .5))
                att = summarize_counts(aggregate_counts(counts["attacked"]))
                nominal_rate = summarize_counts(aggregate_counts(counts["nominal"]))["false_episodes_per_stream_day"]
                rows.append({"variable": VARIABLES[v], "confirmation": mode, "alpha": alpha, **att,
                             "nominal_false_episodes_per_stream_day": nominal_rate,
                             "feasible": att["coverage"] >= cfg["minimum_coverage"] and nominal_rate <= cfg["false_episode_budget"]})
    # Stable sort keeps the grid order among exact ties.
    table = pd.DataFrame(rows).sort_values(["f1", "precision"], ascending=False, kind="stable")
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUTPUTS / "threshold_search.csv", index=False)
    policy = []
    for variable in VARIABLES:
        options = table[(table.variable == variable) & table.feasible]
        if options.empty:
            raise ValueError(f"No feasible alert rule for {variable}")
        best = options.iloc[0]
        policy.append({"confirmation": best.confirmation, "alpha": float(best.alpha)})
    development = defaultdict(list)
    for kind, p, labels, eligible in replays:
        development[kind].append(point_counts(alerts(p, policy), labels, eligible, .5))
    write_json(POLICY, {"policy": policy, "variables": VARIABLES, "selection_partition": "2023-11-01/2024-01-01",
                        "development_results": {k: summarize_counts(aggregate_counts(c)) for k, c in development.items()},
                        "artifact_sha256": {p.name: sha256(p) for p in (EXTRACTOR, MODELS, CALIBRATION)}})
    log(f"Frozen policy: {policy}")


def stage_evaluate():
    """Replay 2024 with the frozen detector: nominal plus two attack seeds on the same weather."""
    detector = Detector()
    seeds = {"nominal": None, **config("evaluation")["attack_seeds"]}
    counts, by_variable, events, seconds = defaultdict(list), defaultdict(list), [], []
    for eid, episode in episodes(load_partition("test")):
        log(f"Evaluation 2024: episode {eid}")
        for suite, seed in seeds.items():
            started = time.perf_counter()
            injection = scenarios(episode, detector.extractor, "test", eid, seed) if seed else None
            observed = episode.with_values(injection.observed) if injection else episode
            labels = injection.changed if injection else np.zeros(episode.values.shape, dtype=bool)
            s, ctx = score(detector.models, detector.extractor, observed)
            a = alerts(p_values(s, detector.tails), detector.policy)
            seconds.append(time.perf_counter() - started)
            counts[suite].append(point_counts(a, labels, ctx["eligible"], .5))
            for v, variable in enumerate(VARIABLES):
                by_variable[suite, variable].append(point_counts(a[..., v:v+1], labels[..., v:v+1], ctx["eligible"][..., v:v+1], .5))
            if injection:
                events.extend({"suite": suite, "episode": eid, **e} for e in event_metrics(injection.events, a, labels, .5))
    metrics = {suite: summarize_counts(aggregate_counts(c)) for suite, c in counts.items()}
    metrics["resources"] = {"episode_passes": len(seconds), "mean_seconds_per_14_day_episode": float(np.mean(seconds)),
                            "threads": config("model")["threads"]}
    write_json(OUTPUTS / "metrics.json", metrics)
    pd.DataFrame([{"suite": s, "variable": v, **summarize_counts(aggregate_counts(c))}
                  for (s, v), c in by_variable.items()]).to_csv(OUTPUTS / "variables.csv", index=False)
    frame = pd.DataFrame(events)
    pd.DataFrame([{"suite": s, "family": f, "events": len(g),
                   "event_recall": float(g.loc[g.point_changed > 0, "detected"].mean()),
                   "point_recall": float(g.point_true_positives.sum()/g.point_changed.sum()) if g.point_changed.sum() else None}
                  for (s, f), g in frame.groupby(["suite", "family"])]).to_csv(OUTPUTS / "families.csv", index=False)
    report(metrics)


def report(metrics):
    """Write ``outputs/RESULTS.md`` from the frozen policy and the 2024 metrics."""
    frozen = read_json(POLICY)
    dev = frozen["development_results"]
    pct = lambda x: "n/a" if x is None else f"{100*x:.1f} %"
    lines = ["# Detector results", "", "## Frozen alert policy (chosen on Nov-Dec 2023)", "",
             "| Variable | Confirmation | alpha |", "|---|---|---:|"]
    lines += [f"| {v} | {r['confirmation']} | {r['alpha']} |" for v, r in zip(VARIABLES, frozen["policy"])]
    lines += ["", f"Development: precision {pct(dev['attacked']['precision'])}, recall {pct(dev['attacked']['recall_end_to_end'])}, "
              f"F1 {dev['attacked']['f1']:.3f}, nominal false episodes/stream-day {dev['nominal']['false_episodes_per_stream_day']:.4f}.",
              "", "## 2024 replay", "", "| Attack seed | Precision | Recall | F1 | Nominal false episodes / stream-day |",
              "|---|---:|---:|---:|---:|"]
    for suite in config("evaluation")["attack_seeds"]:
        m = metrics[suite]
        lines.append(f"| {suite} | {pct(m['precision'])} | {pct(m['recall_end_to_end'])} | {m['f1']:.3f} | "
                     f"{metrics['nominal']['false_episodes_per_stream_day']:.4f} |")
    lines += ["", f"Mean scoring time per 14-day episode: {metrics['resources']['mean_seconds_per_14_day_episode']:.1f} s.",
              "Per-variable and per-family results: `variables.csv`, `families.csv`.", ""]
    (OUTPUTS / "RESULTS.md").write_text("\n".join(lines))
    log(f"Wrote {OUTPUTS / 'RESULTS.md'}")


RUNNERS = {"extract": lambda: extract(), "features": stage_features, "train": stage_train,
           "calibrate": stage_calibrate, "select": stage_select, "evaluate": stage_evaluate}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the Layer 3 detector pipeline.")
    parser.add_argument("--stage", choices=[*STAGES, "all"], default="all")
    parser.add_argument("--force", action="store_true", help="rerun even if the stage output exists")
    args = parser.parse_args(argv)
    for name in (STAGES if args.stage == "all" else [args.stage]):
        if STAGES[name].exists() and not args.force and args.stage == "all":
            log(f"Reuse stage {name} ({STAGES[name].relative_to(ROOT)} exists)")
            continue
        log(f"Run stage {name}")
        RUNNERS[name]()


if __name__ == "__main__":
    main()
