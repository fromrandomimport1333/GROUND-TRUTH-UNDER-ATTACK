"""Latency, memory and throughput measurement for detector pipelines.

`measure` times any zero-argument callable. The CLI wraps a teammate pipeline
given as `module:function`; that function is called once per timed run with the
suite name and must run the detector end to end on that suite.

CLI: uv run python -m eval.overhead --pipeline module:function --suite dev
     -> results/<run>/overhead.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import resource
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_dir(run: str | None) -> Path:
    """results/<run>/, created on demand. Default run name is a UTC timestamp."""
    name = run or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = REPO_ROOT / "results" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def host_info() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is KiB on Linux and bytes on macOS.
    return int(peak if sys.platform == "darwin" else peak * 1024)


def measure(
    run_once: Callable[[], object], *, repeats: int, warmup: int = 1
) -> dict[str, Any]:
    """Time `run_once` and report latency, throughput and memory.

    `warmup` calls are discarded before timing. Latencies are wall-clock
    milliseconds per call. `peak_tracemalloc_bytes` is the peak traced Python
    allocation during one extra call, run outside the timed loop because
    tracemalloc slows allocation-heavy code. `peak_rss_bytes` is the process
    high-water mark. Returned keys are fixed; only the values vary.
    """
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    if warmup < 0:
        raise ValueError("warmup must be >= 0")

    for _ in range(warmup):
        run_once()

    times_ms = np.empty(repeats, dtype=float)
    for i in range(repeats):
        t0 = time.perf_counter_ns()
        run_once()
        times_ms[i] = (time.perf_counter_ns() - t0) / 1e6

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        run_once()
        _, peak_traced = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    total_s = max(float(times_ms.sum()) / 1e3, 1e-9)
    return {
        "n_repeats": repeats,
        "p50_ms": float(np.percentile(times_ms, 50)),
        "p95_ms": float(np.percentile(times_ms, 95)),
        "mean_ms": float(times_ms.mean()),
        "throughput_calls_per_s": float(repeats / total_s),
        "peak_tracemalloc_bytes": int(peak_traced),
        "peak_rss_bytes": _peak_rss_bytes(),
    }


def _load_pipeline(spec: str) -> Callable[[str], object]:
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise SystemExit(f"--pipeline must be module:function, got {spec!r}")
    fn = getattr(importlib.import_module(module_name), attr)
    if not callable(fn):
        raise SystemExit(f"{spec!r} is not callable")
    return fn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pipeline",
        required=True,
        help="module:function, called as fn(suite) once per timed run",
    )
    parser.add_argument("--suite", required=True, help="suite name passed to the pipeline")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--run", default=None, help="results/<run>/ folder (default: UTC timestamp)"
    )
    args = parser.parse_args(argv)

    pipeline = _load_pipeline(args.pipeline)
    result = measure(
        lambda: pipeline(args.suite), repeats=args.repeats, warmup=args.warmup
    )

    out_dir = run_dir(args.run)
    payload = {
        "pipeline": args.pipeline,
        "suite": args.suite,
        "warmup": args.warmup,
        **result,
        "host": host_info(),
    }
    path = out_dir / "overhead.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
