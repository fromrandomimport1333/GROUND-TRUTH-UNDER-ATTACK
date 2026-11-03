"""Layer 4 on its two inputs (Layer 1 verdicts + Layer 3 score rows): trust, hysteresis, actions,
release/imputation and the hash-chained audit log."""
import itertools
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core.contracts import SignedReading, VerifiedReading
from core.response.auditlog import AuditLog
from core.response.trust import ResponseEngine, TrustPolicy, alphas_from_policy, grid_neighbors

T0 = datetime(2023, 11, 1, 0, 30, tzinfo=timezone.utc)
CELL = "wn_grid_y8_x8"
SITE = "grid_y008_x008"
PEERS = list(grid_neighbors(CELL))
Q = {"T_zAvgTerr_HHM": ("K", 280.0), "RH_HHM": ("%", 60.0), "Precip_HHS": ("mm", 0.0)}
SIGNATURE = {"valid": True, "bad_signature": False, "replay": True, "gap": True, "chain_anomaly": True,
             "out_of_window": True, "unknown_sensor": None, "revoked_key": None}
_ids = itertools.count(1)


def at(t):
    return T0 + timedelta(minutes=30*t)


def verdict(sensor, quantity, value, t, status="valid", stamp=None):
    when = at(t if stamp is None else stamp)
    raw = SignedReading(schema_version="1.0", sensor_id=sensor, key_id="k", stream_id=quantity,
                        timestamp=when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), sequence=0, quantity=quantity,
                        value=float(value), unit=Q[quantity][0], prev_hash=b"\0"*32, signature=b"\0"*64)
    return next(_ids), VerifiedReading(schema_version="1.0", reading=raw, status=status,
                                       signature_verified=SIGNATURE[status], checked_at=at(t))


def tick(t, status=None, temperature=None, peer_temperature=281.0, sensors=None):
    """All L1 verdicts of one tick: the centre cell plus its four grid neighbours."""
    status = status or {}
    out = []
    for sensor in sensors or [CELL, *PEERS]:
        for q, (_, v) in Q.items():
            value = v
            if q == "T_zAvgTerr_HHM":
                value = temperature if sensor == CELL and temperature is not None else (peer_temperature if sensor != CELL else v)
            out.append(verdict(sensor, q, value, t, status.get(q, "valid") if sensor == CELL else "valid"))
    return out


def scores(t, alert=False, p=1.0, variable="temperature_c", site=SITE):
    return pd.DataFrame([{"time": pd.Timestamp(at(t)).tz_localize(None), "site_id": site, "variable": v,
                          "p_value": (1e-6 if alert else p) if v == variable else 1.0, "alert": alert and v == variable}
                         for v in ("temperature_c", "relative_humidity_pct", "precip_mm_30min")])


def decide(engine, t, verdicts, rows):
    for event_id, v in verdicts:
        engine.on_verdict(event_id, v)
    return {d.sensor_id: d for d in engine.step(at(t), rows)}


@pytest.fixture
def engine(tmp_path):
    log = AuditLog(tmp_path / "audit.sqlite3")
    yield ResponseEngine(audit=log, alphas={"temperature_c": 1e-3, "relative_humidity_pct": 1e-3, "precip_mm_30min": 1e-3})
    log.close()


def test_nominal_tick_releases_raw_and_is_audited(engine):
    d = decide(engine, 0, tick(0), scores(0))[CELL]
    assert (d.state, d.category, d.actions, d.cause, d.trust_score) == ("trusted", "nominal", (), None, 1.0)
    assert [e.release for e in d.evidence] == ["raw"] * 3
    assert d.evidence[0].released_value == d.evidence[0].raw_value == 280.0
    assert d.evidence[0].event_id is not None
    ok, count, _ = engine.audit.verify()
    assert ok and count == 5                                  # centre + 4 neighbours decided
    assert engine.audit.verify()[0]


def test_single_bad_signature_flags_but_does_not_silence_the_sensor(engine):
    d = decide(engine, 0, tick(0, status={"T_zAvgTerr_HHM": "bad_signature"}, temperature=350.0), scores(0))[CELL]
    assert (d.state, d.category, d.cause) == ("trusted", "provenance_failure", None)
    assert set(d.actions) == {"flag", "notify", "impute"} and d.provenance_failures_recent == 1
    t = d.evidence[0]
    assert t.provenance_status == "bad_signature" and t.raw_value == 350.0          # raw always kept
    assert (t.release, t.released_value, t.imputation_neighbors) == ("imputed", 281.0, 4)   # forged value never released
    assert [e.release for e in d.evidence[1:]] == ["raw", "raw"]                      # genuine readings still flow
    (event,) = engine.audit.entries("provenance_event")
    assert event["payload"]["status"] == "bad_signature" and event["sensor_id"] == CELL


def test_repeated_failures_quarantine_and_need_an_analyst(engine):
    bad = {"T_zAvgTerr_HHM": "bad_signature", "RH_HHM": "bad_signature"}
    decide(engine, 0, tick(0, status=bad), scores(0))                       # 2 failures
    d = decide(engine, 1, tick(1, status={"Precip_HHS": "replay"}), scores(1))[CELL]   # 3rd within 2 ticks
    assert (d.state, d.cause, d.trust_score) == ("quarantined", "provenance", 0.0)
    assert {"quarantine", "notify"} <= set(d.actions) and "analyst must release" in d.explanation
    for t in range(2, 40):                                                   # long clean period: still quarantined
        d = decide(engine, t, tick(t), scores(t))[CELL]
    assert d.state == "quarantined" and all(e.release == "imputed" for e in d.evidence)
    with pytest.raises(ValueError):
        engine.release(CELL, "", "checked")
    assert engine.release(CELL, "analyst-1", "keys rotated, sensor inspected")
    d = decide(engine, 40, tick(40), scores(40))[CELL]
    assert d.state == "trusted" and d.evidence[0].release == "raw"
    assert [e["payload"]["analyst"] for e in engine.audit.entries("analyst_release_applied")] == ["analyst-1"]


def test_failure_together_with_an_alert_quarantines_at_once(engine):
    d = decide(engine, 0, tick(0, status={"RH_HHM": "bad_signature"}), scores(0, alert=True))[CELL]
    assert (d.state, d.cause) == ("quarantined", "provenance")


def test_dashboard_release_request_through_the_audit_log(engine):
    for t, q in enumerate(["T_zAvgTerr_HHM", "RH_HHM", "Precip_HHS"]):
        decide(engine, t, tick(t, status={q: "bad_signature"}), scores(t))
    assert engine.sensors[CELL].state == "quarantined"
    engine.audit.append(at(3), "analyst_release", {"analyst": "analyst-2", "reason": "verified on site"}, sensor_id=CELL)
    engine.audit.append(at(3), "analyst_release", {"analyst": "analyst-2", "reason": "duplicate"}, sensor_id=PEERS[0])
    d = decide(engine, 3, tick(3), scores(3))[CELL]                        # step() applies pending releases
    assert d.state == "trusted"
    assert len(list(engine.audit.entries("analyst_release_ignored"))) == 1  # the peer was not quarantined
    assert engine.audit.verify()[0]


def test_alert_count_hysteresis_and_automatic_recovery(engine):
    p = engine.policy
    states = [decide(engine, t, tick(t), scores(t, alert=True))[CELL].state for t in range(p.quarantine_alerts)]
    assert states == ["trusted"] * (p.suspicious_alerts - 1) + ["suspicious"] * (p.quarantine_alerts - p.suspicious_alerts) + ["quarantined"]
    states, t = [], p.quarantine_alerts
    while len(states) < 40:
        d = decide(engine, t, tick(t), scores(t))[CELL]
        states.append(d.state)
        t += 1
    first_suspicious, first_trusted = states.index("suspicious"), states.index("trusted")
    assert 0 < first_suspicious < first_trusted                              # quarantined -> suspicious -> trusted
    # The 6 alerts leave the 24-tick window one per clean tick after tick 18; the long count reaches
    # quarantine_exit_alerts (2) on the 22nd clean tick (index 21).
    assert first_suspicious == 21 and first_trusted == 22         # short window already clean (doc: ~11.5 h)
    assert d.cause is None and d.evidence[0].release == "raw"


def test_scattered_alerts_accumulate_but_a_short_burst_does_not_quarantine(engine):
    # Like a real attack: one alert every third tick for 48 ticks.
    states = [decide(engine, t, tick(t), scores(t, alert=t % 3 == 0))[CELL].state for t in range(48)]
    assert "quarantined" in states
    other = ResponseEngine()
    burst = [decide(other, t, tick(t, sensors=[CELL]), scores(t, alert=t in (5, 6, 7)))[CELL].state for t in range(30)]
    assert "quarantined" not in burst and "suspicious" in burst


def test_no_flapping_while_suspicious(engine):
    for t in range(3):
        decide(engine, t, tick(t), scores(t, alert=True))
    states = [decide(engine, t, tick(t), scores(t))[CELL].state for t in range(3, 3 + engine.policy.short_window)]
    # Stays suspicious until the 3 alerts have left the 12-tick window, then trusted.
    assert states[:-1] == ["suspicious"] * (len(states) - 1) and states[-1] == "trusted"


def test_unscored_ticks_do_not_age_the_alert_window(engine):
    for t in range(3):
        decide(engine, t, tick(t), scores(t, alert=True))
    for t in range(3, 50):
        d = decide(engine, t, tick(t), scores(t).iloc[0:0])[CELL]
        assert d.category == "unscored"
    assert d.state == "suspicious" and d.alert_count_short == 3


def test_gap_is_noted_not_counted(engine):
    d = decide(engine, 0, tick(0, status={"RH_HHM": "gap"}), scores(0))[CELL]
    assert d.category == "nominal" and d.state == "trusted" and d.trust_score == 1.0
    assert "gap" in d.explanation


def test_imputation_uses_only_trusted_neighbours_this_tick(engine):
    for peer in PEERS[:3]:
        engine.sensors[peer].state, engine.sensors[peer].cause = "quarantined", "provenance"
    d = decide(engine, 0, tick(0, status={"T_zAvgTerr_HHM": "replay"}), scores(0))[CELL]
    t = d.evidence[0]
    assert (t.release, t.imputation_neighbors) == ("withheld", 1)            # one trusted peer < min_neighbors
    assert [e.release for e in d.evidence[1:]] == ["raw", "raw"]
    assert "impute" not in d.actions


def test_replayed_old_record_counts_when_it_arrives(engine):
    decide(engine, 0, tick(0), scores(0))
    old = verdict(CELL, "T_zAvgTerr_HHM", 280.0, 5, status="replay", stamp=0)
    d = decide(engine, 5, [old] + tick(5), scores(5))[CELL]
    assert d.category == "provenance_failure" and d.provenance_failures_recent == 1
    assert d.evidence[0].provenance_status == "valid" and d.evidence[0].release == "raw"   # tick's own reading was fine


def test_future_readings_wait_and_unknown_or_malformed_create_no_sensor(engine):
    engine.on_verdict(*verdict(CELL, "RH_HHM", 60, 1))
    engine.on_verdict(*verdict("intruder_1", "RH_HHM", 60, 0, status="unknown_sensor"))
    junk = VerifiedReading(schema_version="1.0", reading=None, status="malformed", signature_verified=None,
                           checked_at=T0, raw_message_sha256=b"\1"*32)
    engine.on_verdict(99, junk)
    engine.on_verdict(99, junk)
    assert engine.step(at(0), scores(0).iloc[0:0]) == []
    assert "intruder_1" not in engine.sensors
    (d,) = engine.step(at(1), scores(1).iloc[0:0])
    assert d.sensor_id == CELL and d.evidence[1].provenance_status == "valid"
    assert [e["payload"]["status"] for e in engine.audit.entries("provenance_event")] == ["unknown_sensor", "malformed"]


def test_redelivered_verdicts_are_ignored(engine):
    item = verdict(CELL, "T_zAvgTerr_HHM", 280.0, 0, status="bad_signature")
    engine.on_verdict(*item)
    engine.on_verdict(*item)
    assert len(engine.pending) == 1 and len(list(engine.audit.entries("provenance_event"))) == 1


def test_ticks_must_increase_and_policy_validation(engine):
    engine.step(at(1), scores(1))
    with pytest.raises(ValueError, match="increase"):
        engine.step(at(0), scores(0))
    with pytest.raises(ValueError):
        TrustPolicy(suspicious_alerts=0)
    with pytest.raises(ValueError):
        TrustPolicy(quarantine_alerts=30)
    assert alphas_from_policy([{"alpha": 1e-4}, {"alpha": 1e-3}, {"alpha": 1e-2}]) == {
        "temperature_c": 1e-4, "relative_humidity_pct": 1e-3, "precip_mm_30min": 1e-2}
    assert set(grid_neighbors("wn_grid_y0_x5")) == {"wn_grid_y1_x5", "wn_grid_y0_x4", "wn_grid_y0_x6"}
    assert grid_neighbors("sensor-1") == ()


# ---------------------------------------------------------------- real Layer 1 (signer + verifier)

def test_real_layer1_tamper_and_replay(tmp_path, engine):
    """Production signer and verifier: an in-transit edit and a replay are caught by L1 and override L3."""
    from adapters.dataset import Measurement
    from attacks.scenario import DatasetExperiment
    (tmp_path / "l1").mkdir()
    session = DatasetExperiment(tmp_path / "l1", [CELL, *PEERS], "layer4-test", offline_memory=True)
    try:
        payloads = {}

        def sign_tick(t):
            out = []
            for sensor in (CELL, *PEERS):
                for q, (unit, v) in Q.items():
                    m = Measurement(sensor_id=sensor, observed_at=at(t), quantity=q, value=v + t, unit=unit,
                                    source_id="test", source_record_id=f"{sensor}:{q}:{t}")
                    event_id, event, payload = session.verify(m)
                    payloads[(sensor, q, t)] = payload
                    out.append((event_id, event))
            return out

        for t in range(2):
            d = decide(engine, t, sign_tick(t), scores(t))[CELL]
            assert d.state == "trusted" and d.evidence[0].provenance_status == "valid"
        # Tick 2: the centre's temperature value is altered in transit; the signature is left as signed ...
        from core.provenance.codec import decode_reading, encode_reading
        verdicts = sign_tick(2)
        original = decode_reading(payloads[(CELL, "T_zAvgTerr_HHM", 2)])
        tampered = encode_reading(original.model_copy(update={"value": original.value + 5.0}))
        event_id, event = session.verifier.verify(tampered, topic=f"sensors/raw/{CELL}")
        assert event.status == "bad_signature"
        d = decide(engine, 2, verdicts + [(event_id, event)], scores(2))[CELL]
        assert d.category == "provenance_failure" and d.state == "trusted"          # one failure: flag only
        assert d.evidence[0].release == "raw" and d.evidence[0].raw_value == 282.0  # the genuine reading flows
        # ... and at tick 3 an authentic tick-0 message is replayed.
        verdicts = sign_tick(3)
        event_id, event = session.verifier.verify(payloads[(CELL, "RH_HHM", 0)], topic=f"sensors/raw/{CELL}")
        assert event.status == "replay"
        d = decide(engine, 3, verdicts + [(event_id, event)], scores(3))[CELL]
        assert d.category == "provenance_failure" and "RH_HHM=replay" in d.explanation
        assert d.state == "trusted" and d.provenance_failures_recent == 2
        # A third failure within the 4-tick provenance window (another replay) quarantines the sensor.
        event_id, event = session.verifier.verify(payloads[(CELL, "Precip_HHS", 0)], topic=f"sensors/raw/{CELL}")
        d = decide(engine, 4, sign_tick(4) + [(event_id, event)], scores(4))[CELL]
        assert (d.state, d.cause) == ("quarantined", "provenance")
        assert engine.audit.verify()[0]
    finally:
        session.close()


# ---------------------------------------------------------------- audit log

def test_audit_log_is_append_only_and_tamper_evident(tmp_path):
    path = tmp_path / "audit.sqlite3"
    with AuditLog(path) as log:
        for i in range(3):
            log.append(T0, "trust_decision", {"i": i}, sensor_id=CELL)
        assert log.verify() == (True, 3, None)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            log.db.execute("UPDATE audit_log SET payload = '{}' WHERE seq = 2")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            log.db.execute("DELETE FROM audit_log WHERE seq = 3")
    # An attacker with file access drops the trigger and rewrites an entry: the chain breaks.
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER audit_log_no_update")
    db.execute("""UPDATE audit_log SET payload = '{"i":99}' WHERE seq = 2""")
    db.commit()
    db.close()
    with AuditLog(path) as log:
        assert log.verify() == (False, 1, 2)
