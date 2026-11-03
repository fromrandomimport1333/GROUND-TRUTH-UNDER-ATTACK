"""Point- and event-level detection metrics.

Conventions: a point is an (time, site, variable) observation; ``labels`` mark points
the attack *actually* changed; ``eligible`` excludes the warmup. Missing scores count
against end-to-end recall. No point-adjustment: one hit does not mark a whole event as found.
"""
from __future__ import annotations

import numpy as np


def ratio(numerator, denominator):
    """Safe division returning None when the denominator is zero."""
    return float(numerator/denominator) if denominator else None


def summarize_counts(counts):
    """Derive precision, recall (scored-only and end-to-end), F1, false-positive rate, coverage,
    prevalence and false-alert *episodes* per stream-day (48 half-hours = 1 day) from raw counts.
    """
    c = dict(counts)
    c.update(precision=ratio(c["tp"], c["tp"]+c["fp"]),
             recall_conditional=ratio(c["tp"], c["tp"]+c["fn_scored"]),
             recall_end_to_end=ratio(c["tp"], c["positive"]),
             f1=ratio(2*c["tp"], 2*c["tp"]+c["fp"]+c["positive"]-c["tp"]),
             false_positive_rate=ratio(c["fp"], c["negative_scored"]),
             coverage=ratio(c["scored"], c["eligible"]),
             injected_prevalence=ratio(c["positive"], c["eligible"]),
             false_episodes_per_stream_day=ratio(c["false_episodes"], c["negative"]/48),
             false_episodes_per_scored_stream_day=ratio(c["false_episodes"], c["negative_scored"]/48))
    return c


def point_counts(p_values, labels, eligible, alpha):
    """Count TP/FP/FN, positives/negatives, coverage and false-alert episodes at threshold ``alpha``.

    A false-alert episode is a run of consecutive false alerts on the same stream (counted once at its start).
    """
    valid = np.isfinite(p_values) & eligible
    alert = valid & (p_values <= alpha)
    false = alert & ~labels
    previous = np.concatenate([np.zeros_like(false[:1]), false[:-1]], axis=0)
    counts = {"tp": int((alert & labels).sum()), "fp": int(false.sum()),
              "fn_scored": int((valid & labels & ~alert).sum()), "positive": int((labels & eligible).sum()),
              "negative": int((~labels & eligible).sum()), "negative_scored": int((~labels & valid).sum()),
              "eligible": int(eligible.sum()), "scored": int(valid.sum()),
              "false_episodes": int((false & ~previous).sum())}
    return counts


def event_metrics(events, p_values, labels, alpha):
    """Per-attack-event detection: detected flag, first-detection delay, stream-level and point-level recall."""
    result = []
    for event in events:
        start, end = event["start"], event["end"]
        ix = np.ix_(np.arange(start, end), event["sites"], event["variables"])
        changed = labels[ix]
        alerts = np.isfinite(p_values[ix]) & (p_values[ix] <= alpha)
        affected = changed.any(axis=0)
        hit = alerts & affected[None]
        detected = bool(hit.any()) if affected.any() else None
        delay = int(np.flatnonzero(hit.any(axis=(1, 2)))[0]) if detected else None
        result.append({**event, "detected": detected, "delay_steps": delay,
                       "delay_minutes": delay*30 if delay is not None else None,
                       "affected_streams": int(affected.sum()),
                       "detected_streams": int((alerts.any(axis=0) & affected).sum()),
                       "point_changed": int(changed.sum()), "point_true_positives": int((alerts & changed).sum()),
                       "point_recall": ratio(int((alerts & changed).sum()), int(changed.sum())),
                       "detected_within_60_minutes": bool(hit[:3].any()) if affected.any() else None})
    return result


def aggregate_counts(items):
    """Sum count dictionaries (e.g. over episodes)."""
    return {k: sum(item[k] for item in items) for k in items[0]} if items else {}
