# Attack Catalogue

Every fault family in the standard scenario, what it does to the data, which layer is expected
to catch it, and what it would corrupt downstream if it reached the analyst. Sizes are in units
of σ_ref (a station's normal disagreement with its 5 nearest neighbours: ~0.59 °C for temperature,
~2.45 % for humidity) unless a physical unit is given.

## Category A — value faults (signature stays valid; L2/L3 must catch)

| Family | What changes | Params | Expected catch | Carries the catch | Example downstream harm | Status |
|---|---|---|---|---|---|---|
| `offset` | +k·σ added to every reading in the window | k | L3 temporal + spatial | neighbour consistency | biased drought/frost index | ✅ |
| `drift` | error ramps 0→k·σ over the window | k, ramp | L3 temporal | forecast residual (CUSUM) | slow miscalibration of trends | ✅ |
| `spike` | n isolated ±k·σ pulses | k, n | L3 temporal | rate/pulse check | false frost/heat alerts | ✅ |
| `noise` | variance inflated by (k·σ)² | k | L3 temporal + spatial | residual variance | unstable irrigation signals | ✅ |
| `flatline` | variance suppressed (rolling mean, window w) | w | L3 spectral/temporal | variance floor (rules weak) | hides real variability | ✅ |
| `stuck` | last value held constant | — | L3 temporal | constancy check | sensor "alive" but frozen | ✅ |
| `multi_offset` | same k·σ on a station + its n−1 neighbours | k, n | L3 spatial + cross-source | cross-source (neighbours collude) | regional bias passes neighbour check | ✅ |
| `rain_scale` | precipitation × f | f | L3 cross-source | satellite/reanalysis precip | mis-stated rainfall totals (insurance) | ✅ |
| `rain_suppress` | precipitation → 0 in a wet window | — | L1/L3 temporal | constancy + cross-source | missed rainfall event | ✅ |
| `rain_inject` | triangular event of R mm added when dry | R, D | L3 spatial + cross-source | dry-neighbour disagreement | phantom rainfall (false claim) | ✅ |
| `realistic_replace` | window resampled from the station's own history + k·σ bias | k, generator | L3 spatial + cross-source | neighbour/cross-source (marginals look right) | plausible but false series | ✅ |
| `value_replay` | verbatim copy of the station's readings from `days_back` days ago + noise·σ | days_back, noise | L3 spatial + cross-source | neighbour/cross-source (temporal stats are real) | stale data passed as fresh (strong evasion) | ✅ (heldout) |
| `cycle_shaped` | k·σ bias modulated by the diurnal shape | k | L3 spatial + cross-source | neighbour/cross-source (temporal baseline fooled) | false change hidden in daily cycle | ✅ |
| `network_bias` | whole network shifted by k·σ together | k | **cross-source only** | ERA5 / satellite reference | undetectable with station data alone | ✅ (heldout) |
| `gain` | window mean + g·(value − mean) | g | L3 temporal | residual variance | amplified/damped variability | ✅ (heldout) |
| `intermittent_offset` | k·σ on alternating 1-h blocks | k | L3 temporal + spatial | residual/neighbour | intermittent bias | ✅ (heldout) |

The three families that each defeat *one* detector family by design — `realistic_replace`
(fools marginal/temporal detectors, breaks spatial), `cycle_shaped` (fools the temporal baseline,
breaks spatial), `network_bias` (fools spatial, needs cross-source) — are the "evasion" cases:
they force the detector ensemble to carry the catch on another axis, and the report shows which
one does.

## Category B — record faults in transit (L1 provenance must catch)

| Family | What changes | Expected L1 status |
|---|---|---|
| `tamper_value` | reading altered after signing | `bad_signature` |
| `replay` | old authentic record re-sent | `replay` |
| `drop` | record(s) removed | `gap` (next kept record) |
| `reorder` | records swapped | `gap` / `replay` |
| `shift_timestamp` | timestamp changed | `bad_signature` |

## Category C — stored-data faults (L1 must catch)

| Family | What changes | Expected L1 status |
|---|---|---|
| `rewrite_stored` | stored reading edited after verification | `bad_signature` (stage: storage) |
| `unknown_sensor` | record from an unregistered sensor id | `unknown_sensor` |

B/C faults are caught deterministically by the Ed25519 signature + per-sensor hash chain +
sequence numbers (Layer 1): verifying every reading costs ~0.1 ms (≈11,000/s) and 104 bytes/record.
Category A faults carry a valid signature — the signature proves *who sent* a reading, not that it
is *true* — which is why the L2/L3 detectors exist.
