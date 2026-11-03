"""Build the record-level (category B/C) suite: a signed stream with labelled L1 faults.

    uv run python -m attacks.build_records --signer <module>:<function>

``--signer`` is the teammates' real L1 signer, ``sign(record_without_signature: dict, prev_hash: str)
-> dict`` returning the full signed record. The stream is 5 stations x 1 day of 5-min
``air_temperature`` from ``data/clean/`` (one record per station per step, time-major, sensor id =
station id, per-sensor ``seq`` and hash chain). Seven faults, one per transform in
``attacks.records``, are placed at seeded, well-separated positions and written to
``data/suites/records_bc/records.jsonl`` (one record per line, in arrival order) and
``labels.parquet`` (``idx, fault, expected_status, stage``; ``idx`` = 0-based line of records.jsonl).
Everything else in the stream is clean and unlabelled.
"""

from __future__ import annotations

import argparse
import importlib
import json
import random
from pathlib import Path

import pandas as pd

from attacks.records import apply_faults, build_signed_stream

ROOT = Path(__file__).resolve().parents[1]
CLEAN_TABLE = ROOT / "data" / "clean" / "air_temperature.parquet"
OUT_DIR = ROOT / "data" / "suites" / "records_bc"
UNIT = "degC"
EXCLUDED_STATIONS = {"18"}
FAKE_SENSOR = "unregistered-sensor-001"
# One slot per fault, assigned to ascending stream positions in this order.
FAULT_ORDER = (
    "drop", "tamper_value", "reorder", "replay", "shift_timestamp", "rewrite_stored", "unknown_sensor",
)


def load_signer(spec: str):
    module, _, func = spec.partition(":")
    if not module or not func:
        raise SystemExit(f"--signer must look like module:function, got {spec!r}")
    if module.split(".")[0] == "tests":
        raise SystemExit("the test-only signer must not be used to build the shipped suite")
    return getattr(importlib.import_module(module), func)


def load_readings(table: Path, stations: list[str] | None, day: str) -> list[dict]:
    """5-min readings of one UTC day, time-major (all stations for a step, then the next step)."""
    values = pd.read_parquet(table)
    columns = stations or sorted((c for c in values.columns if c not in EXCLUDED_STATIONS), key=int)[:5]
    start = pd.Timestamp(day, tz="UTC")
    window = values.loc[(values.index >= start) & (values.index < start + pd.Timedelta(days=1)), columns]
    readings = []
    for ts, row in window.iterrows():
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        for station in columns:
            if pd.notna(row[station]):
                readings.append(
                    {"sensor_id": str(station), "timestamp": stamp, "value": round(float(row[station]), 3), "unit": UNIT}
                )
    return readings


def _same_sensor_after(records: list[dict], idx: int, count: int) -> list[int]:
    """``idx`` plus the next ``count - 1`` records of the same sensor."""
    sid, found = records[idx]["sensor_id"], [idx]
    for k in range(idx + 1, len(records)):
        if len(found) == count:
            break
        if records[k]["sensor_id"] == sid:
            found.append(k)
    if len(found) != count:
        raise SystemExit("stream too short to place the faults")
    return found


def make_plan(records: list[dict], seed: int) -> list[tuple[str, dict]]:
    """Seven faults at jittered, evenly spaced positions, ordered for ``apply_faults`` (descending)."""
    n = len(records)
    if n < 400:
        raise SystemExit(f"stream of {n} records is too short; need 5 stations x a full day")
    rng = random.Random(seed)
    slots = [int(n * (k + 1) / 8) + rng.randrange(max(n // 40, 1)) for k in range(len(FAULT_ORDER))]
    by_fault = dict(zip(FAULT_ORDER, slots))
    kwargs = {
        "drop": lambda s: {"idxs": _same_sensor_after(records, s, 3)},
        "tamper_value": lambda s: {"idx": s, "delta": 5.0},
        "reorder": lambda s: {"i": s, "j": _same_sensor_after(records, s, 2)[1]},
        "replay": lambda s: {"src_idx": s - n // 14, "at_idx": s},
        "shift_timestamp": lambda s: {"idx": s, "delta_s": 600},
        "rewrite_stored": lambda s: {"idx": s, "delta": -3.0},
        "unknown_sensor": lambda s: {"idx": s, "fake_id": FAKE_SENSOR},
    }
    return [(name, kwargs[name](slot)) for name, slot in sorted(by_fault.items(), key=lambda kv: -kv[1])]


def build(signer, table: Path, out_dir: Path, stations: list[str] | None, day: str, seed: int):
    readings = load_readings(table, stations, day)
    clean = build_signed_stream(readings, signer)
    records, labels = apply_faults(clean, make_plan(clean, seed))
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "records.jsonl", "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    frame = pd.DataFrame(labels, columns=["idx", "fault", "expected_status", "stage"])
    frame["idx"] = frame["idx"].astype("int64")
    frame.to_parquet(out_dir / "labels.parquet", index=False, compression="zstd")
    return records, frame


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--signer", required=True, help="module:function, the L1 signer")
    ap.add_argument("--table", type=Path, default=CLEAN_TABLE, help="clean air_temperature parquet")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--stations", help="comma-separated station ids (default: first 5 by id, minus 18)")
    ap.add_argument("--day", default="2024-06-15", help="UTC day, YYYY-MM-DD")
    ap.add_argument("--seed", type=int, default=2026, help="fault placement seed")
    args = ap.parse_args(argv)
    stations = args.stations.split(",") if args.stations else None
    records, labels = build(load_signer(args.signer), args.table, args.out, stations, args.day, args.seed)
    print(f"{len(records)} records, {len(labels)} labelled -> {args.out}")
    print(labels.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
