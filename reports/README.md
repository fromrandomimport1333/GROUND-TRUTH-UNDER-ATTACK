# Measurement reports

## Current

| File | Content | Produced by |
|---|---|---|
| `detector-overhead.json` | Layer 3 latency (live tick, batch episode), load time and peak memory | `python -m eval.detector_overhead` |
| `phase6-benchmark-linux.json` | Layer 1–2 cost: signing, verification, durable state, processing, message sizes | `python benchmark.py` |
| `phase6-stability-linux.json` | Soak test: three sensors over mutual TLS with injected faults and recovery | `python stability.py` |
| `environment.json` | Interpreter and dependency versions of the Layer 1–2 measurement machine | recorded with the Phase 6 benchmark |

Layer 3 detection quality is not here: it is produced by `python -m core.detection.pipeline --stage evaluate`
(written to `outputs/`, git-ignored) and the reference copy is in `core/results/`.

## Historical

`phase*-verification.md`, `phase*-test-results.txt`, `verification.md` and `windows-*` are development logs
written while Layers 1–2 were built. Their "pending" statements describe that moment; current status is in
[docs/requirements.md](../docs/requirements.md).
