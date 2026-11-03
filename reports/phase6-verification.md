> **Historical development log.** Written while this layer was built; statements about work "pending" or "not yet" implemented describe that moment and are superseded. Current status: [README](../README.md) and [requirements.md](../docs/requirements.md).

# Phase 6 verification — 21 October 2026

## Actual automated tests

Linux, CPython 3.14.8, the existing hash-locked dependencies and real Mosquitto 2.1.2.

```text
python -m pytest -q tests --run-broker --broker-executable PATH_TO_MOSQUITTO
387 passed in 18.45s
```

No skips or failures in the final run. All 361 prior cases are retained, with 26 new Phase 6 cases. All 15 broker tests ran. The full terminal log is phase6-test-results.txt.

An intermediate full run exposed an older receiver test double missing the new connected Event. The test double was updated to model the actual connection state; its assertion that verification/storage failure must not ACK MQTT was retained. The final suite passed.

The focused initial Phase 6 run passed 24 tests in 2.79s. Two additional cases verify idle-timeout behavior and automatic real-broker receiver reconnection. Archive create/status CLI smoke checks also succeeded.

## Actual benchmark

Command: `python benchmark.py --iterations 50 --series 3 --warmup 5 --broker-executable PATH_TO_MOSQUITTO`. The full report is phase6-benchmark-linux.json. Offline stages have 150 measured samples each. TLS/plain transport stages also have 150 publication samples per mode; connection setup has 15 measured samples per mode. Warm-up is excluded.

| Operation | Mean (ms) | p95 (ms) |
| --- | ---: | ---: |
| ed25519_sign_primitive | 0.0331 | 0.0431 |
| ed25519_verify_primitive | 0.1064 | 0.1286 |
| bounded_cbor_decode_and_contract | 0.0308 | 0.0465 |
| chain_record_hash | 0.0141 | 0.0180 |
| sign_with_chain_and_durable_outbox | 1.5784 | 2.0783 |
| verify_with_decode_policy_and_durable_journal | 1.7973 | 2.5143 |
| layer2_16_reading_window | 1.5725 | 1.8100 |

| Transport stage | mTLS mean (ms) | Plain loopback mean (ms) |
| --- | ---: | ---: |
| connection_setup_to_connack | 47.9858 | 0.5711 |
| publish_to_broker_puback | 0.3178 | 0.2466 |
| publish_to_subscriber_callback | 0.2661 | 0.1706 |

This sample occupied 328 bytes as signed CBOR, 208 bytes without signature/predecessor fields, and 471 bytes as JSON with hex binary fields. These are measured envelope sizes, not total network/TLS packet sizes.

The separate 50-iteration resource pass used 0.2162 process CPU seconds over 0.2162 wall seconds, with 172326 bytes peak traced Python allocation. RSS/native memory and lifetime peak observations are retained in the JSON. Broker resources are excluded. These results are specific to this machine and synthetic workload, not performance guarantees.

## Actual two-minute stability run

Command: `python stability.py --broker-executable PATH_TO_MOSQUITTO --duration 120 --interval 0.2`.
Full output: phase6-stability-linux.json.

- Actual elapsed time: 120.185 seconds; requested duration reached.
- Three concurrent sensors, 501 rounds, 1504 issued/valid records.
- Expected injected-fault verdicts: 1 gap and 2 replays.
- All five controlled fault/recovery scenarios exercised.
- Sender and receiver sequence heads matched; zero sender records and zero receiver events pending.
- Recovery archive retained all 1504 signed records, capacity 6010.
- 2 closed feature windows constructed; 15 events in still-open windows retained as unassigned.
- Each publisher made two connections, including recovery after broker restart.
- Python process CPU: 9.008 seconds, 7.49% relative to one core over the run. Broker resources excluded.
- Receiver journal: 1265664 bytes; archive: 753664 bytes at completion. Growth is reported, not hidden.

This establishes a completed two-minute local test, not a multi-hour soak, indefinite reliability or recovery beyond the freshness window. Original timestamps and cryptographic checks remained active. The final unassigned events await their real event-time window closure; they are not discarded.

## Windows evidence

User-reported Phase 5 result: **360 passed, 1 skipped in 57.40s**, with all broker tests enabled; the Phase 5 attack demo also succeeded. The sole skip was the existing symlink privilege test. Native Windows Phase 6 execution remains pending. Commands are in docs/windows-phase6.md.

## Evaluation limits

Metrics formulas were checked against explicitly labeled examples. No real detector performance was evaluated. Real data, the standard attack/evasion scenario, held-out splits, actual Layer 3 predictions and full Layer 4 response integration remain pending. The real benchmark and stability runs concern Layers 1–2 and controlled local transport. Phase 7 has not started.
