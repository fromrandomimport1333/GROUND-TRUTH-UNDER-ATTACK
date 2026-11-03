"""Build a demo Layer 4 audit log for the dashboard.

    python -m scripts.demo_layer4 [--out local/layer4-demo.sqlite3]

Layer 1 is real: the production signer and verifier (public OFFLINE TEST keys) sign and verify
every reading of a 3x3 grid of sensors for two days. Layer 3 is a STAND-IN: until a trained detector
is available, its rows are generated here in the real ``score_stream`` format and marked as synthetic
in the audit log. Scenarios:

* wn_grid_y2_x2: +3 K bias on temperature from tick 40 to 70, signed at the source (signature valid);
  the stand-in Layer 3 alerts on about one tick in three, like the real detector's 2024 point recall.
* wn_grid_y1_x3: three forged (tampered in transit) messages within two ticks -> provenance quarantine.
* wn_grid_y3_x1: one replayed old message at tick 30 -> flagged only.
* every sensor: rare short false-alert bursts, as measured on clean 2024 data.
"""
from __future__ import annotations

import argparse
import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from adapters.dataset import Measurement
from attacks.scenario import DatasetExperiment
from core.provenance.codec import decode_reading, encode_reading
from core.response.auditlog import AuditLog
from core.response.trust import ResponseEngine

T0 = datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc)
TICKS = 96
SENSORS = [f"wn_grid_y{y}_x{x}" for y in (1, 2, 3) for x in (1, 2, 3)]
QUANTITIES = {"T_zAvgTerr_HHM": "K", "RH_HHM": "%", "Precip_HHS": "mm"}
VARIABLES = {"T_zAvgTerr_HHM": "temperature_c", "RH_HHM": "relative_humidity_pct", "Precip_HHS": "precip_mm_30min"}
BIASED, FORGED, REPLAYED = "wn_grid_y2_x2", "wn_grid_y1_x3", "wn_grid_y3_x1"
ATTACK = range(40, 71)


def truth(sensor, quantity, t, rng):
    """Smooth synthetic weather: diurnal temperature/humidity, rain on day 2 afternoon."""
    y, x = int(sensor.split("_y")[1].split("_")[0]), int(sensor.split("_x")[1])
    phase = 2 * math.pi * (t / 48)
    if quantity == "T_zAvgTerr_HHM":
        return 275 + 4 * math.sin(phase - 2) + 0.2 * (y + x) + rng.normal(0, 0.1)
    if quantity == "RH_HHM":
        return min(100.0, 75 - 12 * math.sin(phase - 2) + rng.normal(0, 0.5))
    return max(0.0, rng.normal(0.4, 0.2)) if 70 <= t <= 80 else 0.0


def build(out):
    rng = np.random.default_rng(7)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with tempfile.TemporaryDirectory() as td, AuditLog(out) as log:
        session = DatasetExperiment(Path(td), SENSORS, "layer4-dashboard-demo", offline_memory=True)
        engine = ResponseEngine(audit=log, alphas={v: 1e-3 for v in VARIABLES.values()})
        log.append(T0, "demo_notice", {"layer1": "real signer/verifier, public OFFLINE TEST keys",
                                       "layer3": "SYNTHETIC stand-in rows (no trained detector yet)"})
        payloads = {}
        try:
            for t in range(TICKS):
                ts = T0 + timedelta(minutes=30 * t)
                verdicts = []
                for sensor in SENSORS:
                    for quantity, unit in QUANTITIES.items():
                        value = truth(sensor, quantity, t, rng)
                        if sensor == BIASED and quantity == "T_zAvgTerr_HHM" and t in ATTACK:
                            value += 3.0                     # injected before signing: signature stays valid
                        m = Measurement(sensor_id=sensor, observed_at=ts, quantity=quantity, value=float(value),
                                        unit=unit, source_id="demo", source_record_id=f"{sensor}:{quantity}:{t}")
                        event_id, event, payload = session.verify(m)
                        payloads[(sensor, quantity, t)] = payload
                        verdicts.append((event_id, event))
                if t in (60, 61):                            # forged values in transit (signature not redone)
                    for quantity in (("T_zAvgTerr_HHM", "RH_HHM") if t == 60 else ("Precip_HHS",)):
                        original = decode_reading(payloads[(FORGED, quantity, t)])
                        forged = encode_reading(original.model_copy(update={"value": original.value + 10.0}))
                        verdicts.append(session.verifier.verify(forged, topic=f"sensors/raw/{FORGED}"))
                if t == 30:                                  # an authentic old record replayed
                    verdicts.append(session.verifier.verify(payloads[(REPLAYED, "RH_HHM", 2)],
                                                            topic=f"sensors/raw/{REPLAYED}"))
                rows = []
                if t >= 48 // 4:                             # stand-in warm-up: first 12 ticks unscored
                    for sensor in SENSORS:
                        y, x = sensor.split("_y")[1].split("_x")
                        for quantity, variable in VARIABLES.items():
                            attacked = sensor == BIASED and quantity == "T_zAvgTerr_HHM" and t in ATTACK
                            alert = bool(attacked and rng.random() < 0.33) or bool(rng.random() < 0.0006)
                            p = float(rng.uniform(1e-6, 1e-4) if alert else rng.uniform(0.01, 1.0))
                            rows.append({"time": pd.Timestamp(ts).tz_localize(None), "site_id": f"grid_y{int(y):03d}_x{int(x):03d}",
                                         "variable": variable, "p_value": p, "alert": alert})
                for event_id, event in verdicts:
                    engine.on_verdict(event_id, event)
                engine.step(ts, pd.DataFrame(rows))
                session.discard_delivered()
        finally:
            session.close()
        ok, count, _ = log.verify()
    print(f"Wrote {out}: {count} audit entries, chain {'OK' if ok else 'BROKEN'}")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="local/layer4-demo.sqlite3")
    build(parser.parse_args(argv).out)


if __name__ == "__main__":
    main()
