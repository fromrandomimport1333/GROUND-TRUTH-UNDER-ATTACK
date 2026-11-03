# Layer 4: trust and response

Layer 4 decides, for every sensor at every 30-minute tick, whether the sensor is **trusted**,
**suspicious** or **quarantined**, which actions to take (flag, quarantine, impute, notify) and which
value downstream consumers may receive. Every decision is written to a hash-chained audit log, and an
analyst dashboard (FastAPI, plain HTML/CSS) reads that log.

| File | Role |
|---|---|
| `core/response/trust.py` | `ResponseEngine`: the decision rule; `TrustPolicy`: its thresholds |
| `core/response/auditlog.py` | `AuditLog`: append-only SQLite table with its own SHA-256 chain |
| `core/contracts.py` | `TrustDecision`, `VariableEvidence` |
| `dashboard/app.py`, `dashboard/static/style.css` | FastAPI analyst dashboard (HTML/CSS) |
| `scripts/demo_layer4.py` | Builds a demo audit log for the dashboard |
| `docs/layer4-thresholds.md` | Every threshold, its value and how it was chosen |
| `eval/layer4_threshold_sim.py` | Simulation behind the threshold values |

## Inputs

Layer 4 uses only two inputs:

1. **Layer 1 verdicts.** Every `VerifiedReading`, through `ResponseEngine.on_verdict(event_id, verdict)`,
   which is the existing L4 sink of `ProvenancePipeline`. These verdicts supply the provenance status,
   the raw signed value and, for imputation, the neighbours' values.
2. **Layer 3 output.** The detector rows exactly as `core.detection.adapter.score_stream` returns them:
   `time, site_id, variable, p_value, alert`.

Layer 2 output is not an input. Layer 3 already consumes it.

## Wiring

```python
from core.pipeline import ProvenancePipeline
from core.response.auditlog import AuditLog
from core.response.trust import ResponseEngine, alphas_from_policy

audit = AuditLog("local/layer4-audit.sqlite3")
engine = ResponseEngine(audit=audit, alphas=alphas_from_policy(detector.policy))
pipeline = ProvenancePipeline(verifier, layer4=engine.on_verdict, layer2=layer2_callback)

# once per tick, after Layer 3 has scored it:
decisions = engine.step(tick_time, layer3_rows)        # list[TrustDecision]
released = ResponseEngine.released(decisions)          # only trusted or imputed values
```

For offline replays, `engine.run(ticks, scores)` takes `(ts, [(event_id, verdict), ...])` per tick.

## What a decision contains

- `state`, `previous_state`, `cause` (`provenance` or `anomaly`), `trust_score` (a 0–1 summary for display)
- `category`, which keeps the two cases the architecture requires reporting separately:
  `provenance_failure` (Layer 1 rejected a reading) and `authentic_but_suspicious`
  (valid signature, Layer 3 alerted), plus `nominal` and `unscored`
- `actions`, the alert counts (`alert_count_short`, `alert_count_long`) and `provenance_failures_recent`
- one `evidence` entry per variable: Layer 1 status and event ID, raw value (always kept), p-value,
  alert, and what was released (`raw`, `imputed` from the median of trusted neighbours, or `withheld`)
- `explanation`: a sentence an analyst can read

The rule and its thresholds are in [layer4-thresholds.md](layer4-thresholds.md).

## Audit log

`audit_log(seq, ts, kind, sensor_id, dedup_key, payload, prev_hash, hash)`. Each hash covers the
previous hash and the entry. SQLite triggers reject `UPDATE` and `DELETE`. `AuditLog.verify()`
recomputes the chain and returns the first modified entry. Entry kinds:

| kind | Written by | Content |
|---|---|---|
| `trust_decision` | engine | the full `TrustDecision` |
| `provenance_event` | engine | every non-valid Layer 1 verdict, including malformed and unknown-sensor messages |
| `analyst_release` | dashboard | a release request: analyst and reason |
| `analyst_release_applied` / `analyst_release_ignored` | engine | outcome of a request |

## Dashboard

FastAPI with server-rendered HTML/CSS: no JavaScript, charts are inline SVG, and the colours follow
the system light/dark theme.

```bash
pip install -r requirements-dashboard.txt
python -m scripts.demo_layer4                                  # optional: demo log in local/layer4-demo.sqlite3
python -m dashboard.app --db local/layer4-demo.sqlite3         # http://127.0.0.1:8000
```

`uvicorn dashboard.app:app` also works and reads the path from the `CIS_AUDIT_DB` environment variable.
`--host` and `--port` change the address. Pages refresh themselves every 30 s (except the release form).

| Page | Shows |
|---|---|
| `/` Sensors | Sensors per state, audit chain status, every sensor's state, trust, alert counts and actions, and all state changes |
| `/sensors/{id}` | Raw signed values vs released values with Layer 3 alerts and Layer 1 failures marked; the trust score over the state history; decisions with explanations |
| `/provenance` | Layer 1 rejections (bad signature, replay, malformed, ...) |
| `/suspicious` | Layer 3 alerts on validly signed readings |
| `/release` | Release form for sensors in provenance quarantine, and the release history |
| `/audit` | The latest 500 entries with their hashes |

A red banner appears on every page if the audit chain no longer verifies.

**JSON API**, for the analyst tools and downstream consumers the architecture mentions:

| Endpoint | Returns |
|---|---|
| `GET /api/sensors` | Latest decision per sensor, worst first |
| `GET /api/sensors/{id}/decisions?limit=` | A sensor's decisions, newest first |
| `GET /api/released?after=` | Released values only (raw from trusted sensors, or imputed); poll with the last `seq` |
| `GET /api/chain` | `{ok, entries, first_bad_seq}` |
| `GET /api/docs` | Interactive API documentation |

**Analyst release flow.** A provenance quarantine never ends on its own. The analyst enters a name and
the reason on `/release`, which appends an `analyst_release` entry. At its next tick the engine reads
new requests (`sync_releases`) and either applies them (the sensor returns to trusted with empty
history) or logs why it ignored them.

**Security.** Browsing only reads the database (read-only SQLite connection); the release form is the
only write. Every value is HTML-escaped. There is no user login: the server binds to 127.0.0.1 by
default. Set `CIS_RELEASE_TOKEN` to require a shared token for releases, and put the dashboard behind an
authenticating proxy (with HTTPS) before exposing it on a network.

## Demo data

`scripts/demo_layer4.py` signs and verifies two days of synthetic readings for a 3×3 grid with the
**real** Layer 1 code (public offline test keys). It contains a +3 K bias on one sensor (valid
signatures), three forged messages on another, and one replay on a third. **Its Layer 3 rows are a
synthetic stand-in**, because the trained detector expects the 162-site grid rather than a 3×3 demo grid; the
dashboard shows a banner saying so. On real data, feed `score_stream` output from the trained detector instead.

## Limitations

- Trust state is held in memory; a restart resets every sensor to trusted. The audit log persists.
- The threshold values come from a simulation based on the reference detector's 2024 results; re-derive
  them on deployment data (see the threshold document).
- Released raw values are the signed values as Layer 1 received them, in source units. Layer 2's unit
  and plausibility checks are not part of Layer 4's input (a deliberate choice).
