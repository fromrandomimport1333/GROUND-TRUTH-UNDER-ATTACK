"""Layer 4: trust state, response actions and value release.

Two inputs, nothing else:

* **Layer 1** — every ``VerifiedReading`` through :meth:`ResponseEngine.on_verdict`, the L4 sink of
  ``core.pipeline.ProvenancePipeline``. It supplies the provenance status, the raw signed value and,
  for imputation, the values of neighbouring sensors.
* **Layer 3** — the detector output exactly as ``core.detection.adapter.score_stream`` returns it:
  rows ``time, site_id, variable, p_value, alert``.

A sensor is a Layer 1 ``sensor_id`` (``wn_grid_y8_x23``); Layer 3 site ``grid_y008_x023`` maps to it by
grid index. Verdicts are buffered as they arrive; :meth:`ResponseEngine.step` closes one tick and
decides. Verdicts *arriving* by the tick count against the sensor (a replayed old record counts when it
arrives, not at its forged timestamp); only valid readings timestamped at the tick supply its value.

The rule (thresholds and their justification: ``docs/layer4-thresholds.md``):

* **Anomaly — alert counts.** A tick is *alerted* when Layer 3 alerted on any variable of the sensor.
  Over the sensor's last scored ticks: >= ``suspicious_alerts`` in ``short_window`` -> suspicious;
  >= ``quarantine_alerts`` in ``long_window`` -> quarantined. Exit: quarantined -> suspicious when the
  long count falls to ``quarantine_exit_alerts``; suspicious -> trusted when the short count reaches
  ``trusted_exit_alerts``. Recovery from anomaly quarantine is automatic.
* **Provenance.** One Layer 1 failure flags, notifies and is audited, but does not quarantine: anyone can
  send junk under a sensor's id, and that must not silence the sensor's genuinely signed readings.
  ``provenance_quarantine_failures`` failures within ``provenance_window`` ticks, or a failure in the
  same tick as a Layer 3 alert, quarantine the sensor. Leaving a provenance quarantine needs an analyst:
  :meth:`ResponseEngine.release` (or a release entry written by the dashboard into the audit log).
  ``gap`` is noted, not counted (a gap is not proof of deletion). ``unknown_sensor``/``malformed``
  name no enrolled sensor: audited only.
* **Release.** A valid, non-alerted reading of a *trusted* sensor is released raw; otherwise the median of
  the same quantity at trusted neighbours with valid readings this tick (``impute``), if at least
  ``min_neighbors``; else withheld. Raw values are always kept in the decision and the audit log.
* ``trust_score`` is a [0, 1] summary of the same counts for display and ranking; the *state* is decided
  by the count thresholds above.
"""
from __future__ import annotations

import statistics
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core.contracts import TrustDecision, VariableEvidence
from core.detection.adapter import DEFAULT_QUANTITIES, _key, grid_index
from core.detection.utils import VARIABLES

FAILURES = frozenset({"bad_signature", "revoked_key", "replay", "out_of_window", "chain_anomaly"})
NO_SENSOR = frozenset({"unknown_sensor", "malformed"})   # audit only
GRID_STEPS = ((-1, 0), (1, 0), (0, -1), (0, 1))


@dataclass(frozen=True)
class TrustPolicy:
    """Layer 4 thresholds. Values and their derivation: docs/layer4-thresholds.md."""
    short_window: int = 12                 # scored ticks (6 h at 30 min)
    suspicious_alerts: int = 3             # alerted ticks in short_window -> suspicious
    trusted_exit_alerts: int = 0           # suspicious -> trusted when the short count is at most this
    long_window: int = 24                  # scored ticks (12 h)
    quarantine_alerts: int = 6             # alerted ticks in long_window -> quarantined
    quarantine_exit_alerts: int = 2        # anomaly quarantine -> suspicious when the long count is at most this
    provenance_window: int = 4             # ticks (2 h)
    provenance_quarantine_failures: int = 3
    min_neighbors: int = 2
    audit_nominal: bool = True             # log every decision, not only non-nominal ones
    dedup_memory: int = 200_000            # recent L1 event IDs remembered to drop redeliveries

    def __post_init__(self):
        if not (0 <= self.trusted_exit_alerts < self.suspicious_alerts <= self.short_window):
            raise ValueError("need 0 <= trusted_exit_alerts < suspicious_alerts <= short_window")
        if not (0 <= self.quarantine_exit_alerts < self.quarantine_alerts <= self.long_window):
            raise ValueError("need 0 <= quarantine_exit_alerts < quarantine_alerts <= long_window")
        if self.long_window < self.short_window:
            raise ValueError("long_window must be at least short_window")
        if self.provenance_window < 1 or self.provenance_quarantine_failures < 1 or self.min_neighbors < 1:
            raise ValueError("provenance_window, provenance_quarantine_failures and min_neighbors must be >= 1")


def alphas_from_policy(policy):
    """``{variable: alpha}`` from a frozen L3 ``Detector.policy`` (list of rules in VARIABLES order)."""
    return {v: float(rule["alpha"]) for v, rule in zip(VARIABLES, policy)}


def grid_neighbors(sensor_id):
    """Four cardinal grid neighbours of a ``wn_grid_y{Y}_x{X}`` sensor; none for other id schemes."""
    try:
        y, x = grid_index(sensor_id)
    except ValueError:
        return ()
    return tuple(f"wn_grid_y{y+dy}_x{x+dx}" for dy, dx in GRID_STEPS if y + dy >= 0 and x + dx >= 0)


def score_index(scores):
    """Index Layer 3 rows as ``{(time_ns, sensor_id): {variable: (p_value, alert)}}``."""
    index = defaultdict(dict)
    if scores is None or len(scores) == 0:
        return index
    for time, site, variable, p, alert in scores[["time", "site_id", "variable", "p_value", "alert"]].itertuples(index=False):
        y, x = grid_index(site)
        index[(np.datetime64(pd.Timestamp(time), "ns"), f"wn_grid_y{y}_x{x}")][variable] = (float(p), bool(alert))
    return index


@dataclass
class SensorTrust:
    state: str = "trusted"
    cause: str | None = None
    alerts: deque = field(default_factory=deque)        # alerted flag of the last long_window scored ticks
    failures: deque = field(default_factory=deque)      # failure count of the last provenance_window ticks
    trust: float = 1.0


@dataclass
class ResponseEngine:
    """Stateful Layer 4. Call :meth:`on_verdict` for every L1 verdict, then :meth:`step` once per tick."""
    policy: TrustPolicy = field(default_factory=TrustPolicy)
    audit: object | None = None                       # core.response.auditlog.AuditLog
    alphas: dict = field(default_factory=dict)
    quantities: dict = field(default_factory=lambda: dict(DEFAULT_QUANTITIES))
    neighbors: dict | None = None                     # {sensor_id: [sensor_id, ...]}; default: grid
    sensors: dict = field(default_factory=lambda: defaultdict(SensorTrust))
    pending: list = field(default_factory=list)
    seen: OrderedDict = field(default_factory=OrderedDict)
    last_tick: object = None
    release_cursor: int = 0                           # last audit seq scanned for dashboard releases

    # ---------------------------------------------------------------- Layer 1 input

    def on_verdict(self, event_id, verdict):
        """``ProvenancePipeline`` L4 sink: audit every non-valid verdict and buffer it for the next step.

        Redelivered event IDs (at-least-once delivery) are ignored.
        """
        if event_id in self.seen:
            return
        self.seen[event_id] = None
        while len(self.seen) > self.policy.dedup_memory:
            self.seen.popitem(last=False)
        raw = verdict.reading
        if verdict.status != "valid" and self.audit is not None:
            self.audit.append(verdict.checked_at, "provenance_event",
                              {"event_id": event_id, "status": verdict.status, "findings": list(verdict.findings),
                               "quantity": raw.quantity if raw else None, "timestamp": raw.timestamp if raw else None,
                               "sequence": raw.sequence if raw else None, "value": raw.value if raw else None,
                               "raw_message_sha256": verdict.raw_message_sha256.hex() if verdict.raw_message_sha256 else None},
                              sensor_id=raw.sensor_id if raw else None, dedup_key=f"verdict:{event_id}")
        if raw is not None and verdict.status not in NO_SENSOR:
            self.pending.append((event_id, verdict))

    # ---------------------------------------------------------------- analyst release

    def release(self, sensor_id, analyst, reason, ts=None):
        """Analyst releases a quarantined sensor back to trusted (required after a provenance quarantine).

        Clears the sensor's alert and failure history and audits who released it and why.
        Returns ``True`` if the sensor was quarantined and is now trusted.
        """
        if not analyst or not reason:
            raise ValueError("A release needs the analyst's name and a reason")
        sensor = self.sensors.get(sensor_id)
        if sensor is None or sensor.state != "quarantined":
            return False
        cause = sensor.cause
        self.sensors[sensor_id] = SensorTrust()
        if self.audit is not None:
            self.audit.append(ts or self.last_tick or "unknown", "analyst_release_applied",
                              {"analyst": analyst, "reason": reason, "previous_cause": cause}, sensor_id=sensor_id)
        return True

    def sync_releases(self):
        """Apply ``analyst_release`` entries written into the audit log (e.g. by the dashboard)."""
        if self.audit is None:
            return
        for entry in self.audit.entries("analyst_release", after=self.release_cursor):
            self.release_cursor = entry["seq"]
            payload = entry["payload"]
            analyst, reason = payload.get("analyst"), payload.get("reason")
            if not (analyst and reason and self.release(entry["sensor_id"], analyst, reason, entry["ts"])):
                self.audit.append(entry["ts"], "analyst_release_ignored",
                                  {"request_seq": entry["seq"], "reason": "sensor not quarantined or request incomplete"},
                                  sensor_id=entry["sensor_id"])

    # ---------------------------------------------------------------- per tick

    def run(self, ticks, scores):
        """Replay helper: ``ticks`` yields ``(ts, [(event_id, verdict), ...])``; yields each tick's decisions."""
        index = score_index(scores)
        for ts, verdicts in ticks:
            for event_id, verdict in verdicts:
                self.on_verdict(event_id, verdict)
            yield self.step(ts, index)

    def step(self, ts, scores):
        """Close tick ``ts`` (the readings' interval-end timestamp) and decide for every sensor that sent
        a verdict or has a Layer 3 row at ``ts``. ``scores``: a ``score_index`` dict or a DataFrame."""
        if not isinstance(scores, dict):
            scores = score_index(scores)
        if ts.tzinfo is None:
            raise ValueError("Tick timestamp must be timezone-aware")
        if self.last_tick is not None and ts <= self.last_tick:
            raise ValueError("Ticks must increase")
        self.sync_releases()
        self.last_tick = ts
        key_time = _key(ts)
        arrived, later = defaultdict(list), []
        for event_id, verdict in self.pending:
            (later.append((event_id, verdict)) if verdict.reading.observed_at > ts
             else arrived[verdict.reading.sensor_id].append((event_id, verdict)))
        self.pending = later
        sensors = set(arrived) | {sid for (t, sid) in scores if t == key_time}
        current = {}
        for sid, items in arrived.items():
            for event_id, verdict in items:
                if verdict.reading.observed_at == ts:
                    current.setdefault((sid, verdict.reading.quantity), []).append((event_id, verdict))
        # Phase 1: state of every sensor, so imputation sees this tick's states.
        staged = [(sid, self._update(sid, arrived.get(sid, []), scores.get((key_time, sid), {})))
                  for sid in sorted(sensors)]
        # Phase 2: release / impute and the decision record.
        decisions = []
        for sid, update in staged:
            decision = self._decide(ts, sid, update, current)
            decisions.append(decision)
            if self.audit is not None and (self.policy.audit_nominal or decision.category != "nominal"
                                           or decision.actions or decision.state != decision.previous_state):
                self.audit.append(ts, "trust_decision", decision.model_dump(mode="json"),
                                  sensor_id=sid, dedup_key=f"decision:{sid}:{ts.isoformat()}")
        return decisions

    def _update(self, sid, items, sensor_scores):
        p = self.policy
        sensor = self.sensors[sid]
        previous = sensor.state
        failed = sorted({(v.reading.quantity, v.status, e) for e, v in items if v.status in FAILURES})
        gap = any(v.status == "gap" for _, v in items)
        per_variable = {}
        for variable in VARIABLES:
            pv, alert = sensor_scores.get(variable, (None, False))
            per_variable[variable] = (pv, alert, self.alphas.get(variable) if pv is not None else None)
        scored = any(pv is not None for pv, _, _ in per_variable.values())
        alerted = any(alert for _, alert, _ in per_variable.values())
        if scored:   # unscored ticks give no evidence and do not age the alert window
            sensor.alerts.append(alerted)
            while len(sensor.alerts) > p.long_window:
                sensor.alerts.popleft()
        sensor.failures.append(len(failed))
        while len(sensor.failures) > p.provenance_window:
            sensor.failures.popleft()
        short = sum(list(sensor.alerts)[-p.short_window:])
        long = sum(sensor.alerts)
        recent_failures = sum(sensor.failures)

        if sensor.state == "quarantined" and sensor.cause == "provenance":
            pass                                                  # only an analyst releases it
        elif failed and (recent_failures >= p.provenance_quarantine_failures or alerted):
            sensor.state, sensor.cause = "quarantined", "provenance"
        elif long >= p.quarantine_alerts:
            sensor.state, sensor.cause = "quarantined", "anomaly"
        elif sensor.state == "quarantined":
            if long <= p.quarantine_exit_alerts:
                sensor.state = "suspicious"
        elif sensor.state == "suspicious":
            if short <= p.trusted_exit_alerts:
                sensor.state, sensor.cause = "trusted", None
        elif short >= p.suspicious_alerts:
            sensor.state, sensor.cause = "suspicious", "anomaly"

        if failed:
            category = "provenance_failure"
        elif not scored:
            category = "unscored"
        else:
            category = "authentic_but_suspicious" if alerted else "nominal"
        if sensor.state == "quarantined" and sensor.cause == "provenance":
            sensor.trust = 0.0
        else:
            pressure = max(0.5 * min(1.0, short / p.suspicious_alerts), min(1.0, long / p.quarantine_alerts))
            sensor.trust = float((1.0 - pressure) * (1.0 - min(1.0, recent_failures / p.provenance_quarantine_failures)))
        stale = sum(1 for _, v in items if v.status == "valid" and v.reading.observed_at != self.last_tick)
        return dict(previous=previous, category=category, failed=failed, gap=gap, stale=stale, short=short,
                    long=long, recent_failures=recent_failures, per_variable=per_variable)

    def _neighbors(self, sid):
        return tuple(self.neighbors.get(sid, ())) if self.neighbors is not None else grid_neighbors(sid)

    @staticmethod
    def _usable(entries):
        """The single valid, non-null reading among a sensor/quantity's tick entries (None if absent/ambiguous)."""
        valid = [(e, v) for e, v in entries or () if v.analysis_eligible]
        return valid[0] if len(valid) == 1 else None

    def _impute(self, sid, quantity, current):
        peers = []
        for peer in self._neighbors(sid):
            usable = self._usable(current.get((peer, quantity)))
            if usable is not None and self.sensors[peer].state == "trusted":
                peers.append(usable[1].reading)
        if len(peers) < self.policy.min_neighbors:
            return None, len(peers), None
        return float(statistics.median(r.value for r in peers)), len(peers), peers[0].unit

    def _decide(self, ts, sid, update, current):
        sensor = self.sensors[sid]
        evidence, imputed, withheld = [], [], []
        for variable in VARIABLES:
            quantity = self.quantities[variable]
            entries = current.get((sid, quantity), [])
            usable = self._usable(entries)
            shown = usable or (entries[0] if entries else None)
            event_id, verdict = shown if shown else (None, None)
            status = verdict.status if verdict else "missing"
            if len(entries) > 1 and usable is None:
                status = "ambiguous"
            raw_value = verdict.reading.value if verdict else None
            unit = verdict.reading.unit if verdict else None
            pv, alert, alpha = update["per_variable"][variable]
            if sensor.state == "trusted" and usable is not None and not alert:
                release, released, n = "raw", raw_value, 0
            else:
                released, n, peer_unit = self._impute(sid, quantity, current)
                unit = unit or peer_unit
                release = "imputed" if released is not None else "withheld"
                (imputed if released is not None else withheld).append(variable)
            evidence.append(VariableEvidence(
                variable=variable, quantity=quantity, provenance_status=status, event_id=event_id,
                raw_value=raw_value, unit=unit, p_value=pv, alpha=alpha, alert=alert,
                anomaly=1.0 if alert else (0.0 if pv is not None else None),
                released_value=released, release=release, imputation_neighbors=n))
        alerted = [v for v, (_, al, _) in update["per_variable"].items() if al]
        entered = sensor.state != update["previous"] and sensor.state != "trusted"
        actions = []
        if update["failed"] or alerted:
            actions.append("flag")
        if sensor.state == "quarantined" and update["previous"] != "quarantined":
            actions.append("quarantine")
        if imputed:
            actions.append("impute")
        if entered or update["failed"]:
            actions.append("notify")
        return TrustDecision(
            sensor_id=sid, ts=ts, trust_score=sensor.trust, state=sensor.state, previous_state=update["previous"],
            category=update["category"], cause=sensor.cause, actions=tuple(actions),
            alert_count_short=update["short"], alert_count_long=update["long"],
            provenance_failures_recent=update["recent_failures"],
            explanation=self._explain(sensor, update, alerted, imputed, withheld), evidence=tuple(evidence))

    def _explain(self, sensor, update, alerted, imputed, withheld):
        p = self.policy
        parts = []
        if update["failed"]:
            detail = ", ".join(f"{q}={s} (event {e})" for q, s, e in update["failed"])
            parts.append(f"Layer 1 rejected {detail}; {update['recent_failures']} provenance failure(s) in the last "
                         f"{p.provenance_window} tick(s).")
        if alerted:
            parts.append("Signature valid but Layer 3 alerted on " + ", ".join(alerted) +
                         " (authentic but suspicious: possible physical spoofing or compromised node).")
        elif update["category"] == "unscored":
            parts.append("Layer 3 did not score this tick (warm-up or incomplete history); no detector evidence.")
        elif not update["failed"]:
            parts.append("Provenance valid and no Layer 3 alert.")
        parts.append(f"Alerted ticks: {update['short']}/{p.short_window} (suspicious at {p.suspicious_alerts}), "
                     f"{update['long']}/{p.long_window} (quarantine at {p.quarantine_alerts}).")
        if update["gap"]:
            parts.append("Layer 1 reported a sequence gap before this reading (noted, not counted).")
        if update["stale"]:
            parts.append(f"{update['stale']} valid reading(s) arrived with a timestamp other than this tick; not used.")
        if sensor.state != update["previous"]:
            parts.append(f"State {update['previous']} -> {sensor.state}.")
        else:
            parts.append(f"State {sensor.state}.")
        if sensor.state == "quarantined" and sensor.cause == "provenance":
            parts.append("Provenance quarantine: an analyst must release this sensor.")
        if imputed:
            parts.append("Released neighbour-median estimate for " + ", ".join(imputed) + "; raw value kept.")
        if withheld:
            parts.append("Withheld " + ", ".join(withheld) + " (no trusted value and too few trusted neighbours).")
        return " ".join(parts)

    # ---------------------------------------------------------------- downstream release

    @staticmethod
    def released(decisions):
        """Downstream view: only raw values from trusted sensors or imputed estimates, never withheld ones."""
        return [{"sensor_id": d.sensor_id, "ts": d.ts, "quantity": e.quantity, "value": e.released_value,
                 "unit": e.unit, "source": e.release, "state": d.state}
                for d in decisions for e in d.evidence if e.release != "withheld"]
