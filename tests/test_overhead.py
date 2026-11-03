import tracemalloc

from eval.overhead import measure

PERCENTILE_KEYS = ("p50_ms", "p95_ms", "mean_ms")
SHAPE_KEYS = (
    "n_repeats",
    "throughput_calls_per_s",
    "peak_tracemalloc_bytes",
    "peak_rss_bytes",
)


def test_measure_reports_percentiles_and_peak():
    def run_once():
        return [0] * 100_000  # ~800 kB live allocation, visible to tracemalloc

    out = measure(run_once, repeats=20, warmup=2)

    for key in PERCENTILE_KEYS + SHAPE_KEYS:
        assert key in out, key
    assert out["n_repeats"] == 20
    assert out["p50_ms"] >= 0.0
    assert out["p95_ms"] >= out["p50_ms"]
    assert out["throughput_calls_per_s"] > 0.0
    assert out["peak_tracemalloc_bytes"] > 500_000
    assert out["peak_rss_bytes"] > 0


def test_crypto_overhead_roundtrip_counts():
    # Imported here so the measure() tests above do not need `cryptography`.
    from eval.bench_crypto import crypto_overhead

    out = crypto_overhead(200)

    assert out["verify_failures"] == 0
    assert out["bytes_per_record"] == 64 + 8 + 32  # signature + seq + prev_hash
    assert out["bytes_per_record"] > 0
    assert out["sign_per_s"] > 0
    assert out["verify_per_s"] > 0
    assert out["tampered_checked"] > 0
    assert out["tampered_rejected"] == out["tampered_checked"]


def test_measure_is_pure_on_noop():
    calls = []

    out = measure(lambda: calls.append(1), repeats=5)

    assert out["n_repeats"] == 5
    assert out["p50_ms"] >= 0.0
    assert len(calls) >= 1 + 5  # warmup (default 1) + timed repeats
    assert not tracemalloc.is_tracing()  # measure() leaves tracing as it found it
