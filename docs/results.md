# Results

Results of the **station attack suite** (5-minute WegenerNet stations) and the security overhead.
The trained Layer 3 detector's results on the half-hourly grid are in
[core/docs/README.md § 5](../core/docs/README.md#5-results); its latency and memory are in
[§ Detector latency and memory](#detector-latency-and-memory) below. The consolidated picture is in the
[Phase 1 report](report.pdf).

## Suite calibration (reference baseline on the dev suite)

The `baseline_combined` reference detector (range/step/constancy rules OR 5-NN consistency)
is a floor, not the team detector. On the real `dev` suite its offset recall rises monotonically
with fault size, confirming the exam is graded, not all-or-nothing:

| offset size (k·σ_ref) | 0.5 | 1 | 2 | 3 | 5 |
|---|---|---|---|---|---|
| recall | 0.31 | 0.31 | 0.38 | 0.50 | 0.81 |

Small faults (k ≤ 1) mostly evade (recall < 0.5, as the design targets); large faults (k = 5)
are mostly caught (0.81). The team detector should exceed 0.9 at k = 5 — the ~0.19 the floor
misses is the headroom it must win. Per-family recall for the floor (dev): drift 0.83,
multi_offset 0.89, stuck 0.75, noise 0.64, spike 0.53, flatline 0.08 (flatline hides variance,
so a rules/neighbour floor is weak — a spectral/temporal detector is needed).

False-alarm rate of the floor on fault-free 2024 (`clean2024`): air temperature ~4.5 alarms
per station-day (0.61 episodes), precipitation ~0.02. Humidity is noisier (the 5-NN rule at
z = 4 fires on real day/night valley differences); the team detector should bring this down —
lower false-alarm rate is the competition's second tie-breaker.

## Cross-source check & the network-wide bias (remote-sensing angle)

A `network_bias` fault shifts every station together. Neighbour consistency is **structurally
blind** to it: when all stations (and therefore all of each station's neighbours) move by the
same amount, every station still agrees with its neighbours. The `crosssource` suite is this
attack (whole network, k = 2 and 3 σ_ref, held out).

The controlled demonstration (unit tests `test_neighbor_baseline_misses_network_bias`,
`test_crosssource_baseline_catches_network_bias`): under a uniform network shift, the neighbour
baseline alarms on < 2 % of cells (its background noise — it does not detect the shift), while
the cross-source check against an independent reference alarms on ~100 % of cells.

Honest limitation worth stating in the report: with **in-situ station data alone**, a coordinated
whole-network bias is not separable from a genuine regional weather event — any internal
reference (even a held-out station cluster) moves with the attack. Separating them **requires an
independent source**: ERA5-Land reanalysis or satellite land-surface temperature. `data_prep/reference.py`
fetches ERA5 when a CDS key is configured and the `crosssource_baseline` detector consumes it;
without a key it documents the requirement. This is precisely why the challenge asks all three
chapters to combine — the remote-sensing input is not optional for this attack class. (The
network spans only ~1–4 ERA5 cells, so the external reference is a network-wide check, not a
per-station one — which the temporal/spatial detectors already cover.)

## Escape magnitude (evasion) — reference baseline

`eval/escape.py` sweeps each family's size and reports the largest the detector still misses.
Against the `baseline_combined` reference (one showcase window per family), the escape magnitudes are:
offset 3·σ_ref, noise 2·σ_ref, cycle_shaped 2·σ_ref, network_bias 2·σ_ref, rain_scale ×3, rain_inject 3 mm·scale.
Every other family was caught at the smallest probed size (NaN = no residual escape, or no size parameter).
Read this as the reference floor's blind spots; the team detector should push these boundaries lower,
and the same command run against it gives the honest "below this, residual risk" line for the report.

## Efficiency & security overhead

Measured with `python -m eval.bench_crypto --n 10000` on the same 2-core x86-64 machine as the detector, Python 3.13,
`cryptography` 50.0.2 (Ed25519), n = 10 000 records. Output: [`reports/crypto-benchmark.json`](../reports/crypto-benchmark.json).
This is the Layer-1 provenance cost — the "security overhead" the brief asks for.

| Metric | Value |
|---|---|
| Ed25519 sign throughput (incl. hash chaining) | ~17,700 records/s |
| Ed25519 verify throughput (incl. chain-link check) | ~8,050 records/s |
| Added bytes per record (sig 64 + seq 8 + prev_hash 32) | 104 B |
| Wire record size (payload 16 B + overhead) | 120 B |
| Verify failures on untampered stream | 0 |
| Tampered (bit-flipped) records rejected | 100 / 100 |

Interpretation: verifying every reading from all 154 stations (one reading per 5 min = 0.51
readings/s network-wide) uses a negligible fraction of one core; provenance is not a
bottleneck on a laptop.

## Detector latency and memory

Measured with `python -m eval.detector_overhead` (output: [`reports/detector-overhead.json`](../reports/detector-overhead.json))
on a 2-core x86-64 Linux machine, Python 3.13, the shipped artifacts, CPU only. The input is synthetic with the
exact production shape (162 sites × 3 variables, 30-minute steps); latency and memory depend on the shape,
not on the values.

| Metric | Value |
|---|---|
| Load and verify the frozen artifacts (SHA-256 check) | 0.33 s |
| **Live tick**: score one new 30-minute step for all 486 streams, with 24 h of history | **median 202 ms, p95 236 ms, max 276 ms** |
| Live tick, per reading | 0.42 ms |
| Real-time headroom | one tick every 30 min = 1,800,000 ms → under 0.02 % of the interval |
| Batch replay: one 14-day episode (303,264 scored readings) | 5.0 s (16.5 µs per reading) |
| Peak resident memory (whole process, batch episode included) | 852 MB |
| Memory after loading the model | 293 MB |
| Model artifacts on disk | 17.6 MB |

The live figure is the latency the challenge asks for: from "the new half-hour of readings is available" to
"p-values and alerts ready for Layer 4". Detection-quality figures for the same model are measured separately on
real 2024 data.
