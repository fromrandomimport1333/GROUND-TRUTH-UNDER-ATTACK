"""Score one detector run against a suite.

    uv run python -m eval.run --suite dev --alarms alarms/<run>/dev [--out results/<run>/dev]

Reads ``data/suites/<suite>/labels.parquet`` and the alarm tables ``<alarms>/{variable}.parquet``
(bool, same index/columns as the suite's value tables). If ``<alarms>/{variable}_state.parquet``
exists (trust states) leakage uses it. Writes ``events.parquet``, ``summary.json`` and ``tables.md``
to ``--out`` (default ``results/<run>/<suite>``).

* ``clean2024``: false alarms per station-day (overall and excluding gap-filled cells), 2024.
* ``records_bc``: reads ``<alarms>/statuses.jsonl`` (``{"idx": int, "status": str}`` per line) and
  scores the L1 verifier with ``score_records``.
* any other suite: event detection, point metrics, sensitivity / MDM90 and release leakage, 2024 only.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from eval.scoring import (
    YEAR_2024,
    event_table,
    false_alarm_rate,
    mdm90,
    point_metrics,
    record_outcomes,
    release_leakage,
    score_records,
    sensitivity,
    sensitivity_long,
)

ROOT = Path(__file__).resolve().parents[1]
VARIABLES = ("air_temperature", "relative_humidity", "precipitation")
YEAR_START, YEAR_END = pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")


# ------------------------------------------------------------------------------ output


def _plain(obj):
    """JSON-safe copy: numpy scalars to Python, NaN/inf to null, keys to str."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        return None if not math.isfinite(obj) else float(obj)
    return obj


def _rows(frame: pd.DataFrame) -> dict:
    return {str(k): _plain(v) for k, v in frame.to_dict(orient="index").items()}


def _cell(value) -> str:
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return "" if math.isnan(value) else f"{value:.3f}"
    return "" if value is None else str(value)


def md_table(frame: pd.DataFrame) -> str:
    """GitHub-flavoured markdown table (the index becomes the first column)."""
    frame = frame.reset_index()
    head = [str(c) if not (isinstance(c, float) and math.isnan(c)) else "n/a" for c in frame.columns]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in frame.itertuples(index=False)]
    return "\n".join(lines)


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body}\n"


# ----------------------------------------------------------------------------- loading


def load_alarms(alarm_dir: Path, variables) -> dict[str, pd.DataFrame]:
    tables = {}
    for var in variables:
        path = alarm_dir / f"{var}.parquet"
        if path.exists():
            tables[var] = pd.read_parquet(path)
    if not tables:
        raise SystemExit(f"no alarm tables {{variable}}.parquet found in {alarm_dir}")
    return tables


def load_states(alarm_dir: Path, variables) -> dict[str, pd.DataFrame]:
    return {v: pd.read_parquet(p) for v in variables if (p := alarm_dir / f"{v}_state.parquet").exists()}


def in_2024(labels: pd.DataFrame) -> pd.DataFrame:
    """Keep labels that start in 2024 (2023 is the calibration year and is never scored)."""
    if labels.empty:
        return labels
    start = pd.DatetimeIndex(labels["start"])
    if start.tz is None:
        raise SystemExit("labels.start is tz-naive; labels must be tz-aware UTC")
    return labels[(start >= YEAR_START) & (start < YEAR_END)].reset_index(drop=True)


def default_out(alarm_dir: Path, suite: str) -> Path:
    run = alarm_dir.parent.name if alarm_dir.name == suite else alarm_dir.name
    return ROOT / "results" / run / suite


# -------------------------------------------------------------------------- scenarios


def _detection_block(events: pd.DataFrame) -> dict:
    per_scenario = events.drop_duplicates("scenario_id")

    def block(frame: pd.DataFrame) -> dict:
        lat, lat_onset = frame["latency_min"], frame["latency_from_onset_min"]
        return {
            "scenarios": len(frame),
            "detected": int(frame["detected"].sum()),
            "recall": float(frame["detected"].mean()) if len(frame) else float("nan"),
            "latency_min_median": lat.median(),
            "latency_min_p90": lat.quantile(0.9),
            "latency_from_onset_min_median": lat_onset.median(),
        }

    return {
        "overall": block(per_scenario),
        "by_family": {f: block(g) for f, g in per_scenario.groupby("family")},
        "by_variable": {v: block(g) for v, g in per_scenario.groupby("variable")},
    }


def run_scenarios(suite, labels, alarms, states, grace) -> tuple[pd.DataFrame, dict, str]:
    events = event_table(labels, alarms, grace=grace)
    points = point_metrics(labels, alarms)
    leak_var = release_leakage(labels, alarms, states=states)
    leak_family = release_leakage(labels, alarms, states=states, by="family")
    sens = sensitivity(events)
    mdm = mdm90(events)
    detection = _detection_block(events)
    state_source = {v: ("state" if v in states else "alarm") for v in sorted(set(labels["variable"]))}
    summary = {
        "suite": suite,
        "period": "2024",
        "grace": grace,
        "n_label_rows": len(events),
        "n_scenarios": int(events["scenario_id"].nunique()),
        "detection": detection,
        "point_metrics": points,
        "sensitivity": {f: {_size(s): r for s, r in row.items() if not math.isnan(r)} for f, row in sens.iterrows()},
        "mdm90": mdm.to_dict(),
        "leakage": {
            "overall": _rows(leak_var).get("overall", {}),
            "by_variable": {k: v for k, v in _rows(leak_var).items() if k != "overall"},
            "by_family": {k: v for k, v in _rows(leak_family).items() if k != "overall"},
            "state_source": state_source,
        },
    }
    fam = pd.DataFrame(detection["by_family"]).T.rename_axis("family").astype({"scenarios": "int64", "detected": "int64"})
    point_table = pd.DataFrame(points).T.rename_axis("variable").astype({"tp": "int64", "fp": "int64", "fn": "int64"})
    tables = "\n".join(
        [
            f"# Scoring: {suite} (2024, grace {grace})\n",
            _section("Detection by family (scenario level)", md_table(fam)),
            _section("MDM90: smallest size with recall >= 0.9", md_table(mdm.to_frame())),
            _section("Recall by family x size", md_table(sens)),
            _section("Recall counts by family x size", md_table(sensitivity_long(events).set_index(["family", "size"]))),
            _section("Point metrics (no point adjustment)", md_table(point_table)),
            _section("Release leakage by variable (state source: " + ", ".join(f"{k}={v}" for k, v in state_source.items()) + ")", md_table(leak_var)),
            _section("Release leakage by family", md_table(leak_family)),
        ]
    )
    return events, summary, tables


def _size(value) -> str:
    return "n/a" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value:g}"


def run_clean(suite, alarms, flags) -> tuple[pd.DataFrame, dict, str]:
    far = false_alarm_rate(alarms, flags, YEAR_2024)
    summary = {"suite": suite, "period": "2024", "false_alarms": _rows(far)}
    tables = f"# False alarms: {suite} (2024)\n\n" + _section("Alarms per station-day", md_table(far))
    return pd.DataFrame(), summary, tables


# --------------------------------------------------------------------------- records


def read_jsonl(path: Path) -> pd.DataFrame:
    with open(path) as fh:
        return pd.DataFrame([json.loads(line) for line in fh if line.strip()])


def run_records(suite, labels, alarm_dir: Path, records_path: Path) -> tuple[pd.DataFrame, dict, str]:
    status_path = alarm_dir / "statuses.jsonl"
    if not status_path.exists():
        raise SystemExit(f"{status_path} not found; the verifier must write {{idx, status}} per line")
    statuses = read_jsonl(status_path)
    note = ""
    if records_path.exists():
        records = read_jsonl(records_path)
        extra = records[[c for c in ("sensor_id", "seq") if c in records.columns]]
        extra = extra.assign(idx=np.arange(len(records)))
        statuses = statuses.drop(columns=[c for c in extra.columns if c != "idx" and c in statuses.columns]).merge(
            extra, on="idx", how="left"
        )
    else:
        note = f"{records_path} not found: follow-on gaps assume a single stream"
        print(note)
    scored = score_records(labels, statuses)
    unlabelled = scored.loc["unlabelled"]
    by_fault = scored.drop(index="unlabelled")
    summary = {
        "suite": suite,
        "records": {
            "by_fault": _rows(by_fault[["expected_status", "n", "matched", "match_rate"]]),
            "unlabelled": int(unlabelled["n"]),
            "false_positives": int(unlabelled["false_positives"]),
            "followon_gaps": int(unlabelled["followon_gaps"]),
            "note": note or None,
        },
    }
    tables = f"# Record-level verification: {suite}\n\n" + _section("Match rate by fault", md_table(scored))
    return record_outcomes(labels, statuses), summary, tables


# ------------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suite", required=True, help="suite name, e.g. smoke, dev, heldout, clean2024, records_bc")
    ap.add_argument("--alarms", required=True, type=Path, help="directory with {variable}.parquet (or statuses.jsonl)")
    ap.add_argument("--out", type=Path, help="default: results/<run>/<suite>")
    ap.add_argument("--suites-dir", type=Path, default=ROOT / "data" / "suites")
    ap.add_argument("--clean-dir", type=Path, default=ROOT / "data" / "clean", help="where {variable}_flag.parquet lives")
    ap.add_argument("--records", type=Path, help="records.jsonl for sensor ids (default: <suites-dir>/<suite>/records.jsonl)")
    ap.add_argument("--grace", default="1h", help="detection grace after the window end (default 1h)")
    ap.add_argument("--variables", help="comma-separated subset of variables to score")
    args = ap.parse_args(argv)

    suite, alarm_dir = args.suite, args.alarms
    out_dir = args.out or default_out(alarm_dir, suite)
    labels_path = args.suites_dir / suite / "labels.parquet"
    if labels_path.exists():
        labels = pd.read_parquet(labels_path)
    elif suite == "clean2024":
        labels = pd.DataFrame()
    else:
        raise SystemExit(f"{labels_path} not found; build the suite first")
    variables = args.variables.split(",") if args.variables else VARIABLES

    if suite == "records_bc":
        records_path = args.records or args.suites_dir / suite / "records.jsonl"
        events, summary, tables = run_records(suite, labels, alarm_dir, records_path)
    else:
        alarms = load_alarms(alarm_dir, variables)
        labels = in_2024(labels)
        if labels.empty:
            flags = {v: pd.read_parquet(args.clean_dir / f"{v}_flag.parquet") for v in alarms}
            events, summary, tables = run_clean(suite, alarms, flags)
        else:
            labels = labels[labels["variable"].isin(variables)]
            events, summary, tables = run_scenarios(suite, labels, alarms, load_states(alarm_dir, variables), args.grace)

    out_dir.mkdir(parents=True, exist_ok=True)
    events.to_parquet(out_dir / "events.parquet", index=False)
    (out_dir / "summary.json").write_text(json.dumps(_plain(summary), indent=2, sort_keys=True) + "\n")
    (out_dir / "tables.md").write_text(tables)
    print(f"{suite}: wrote {out_dir}/{{events.parquet,summary.json,tables.md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
