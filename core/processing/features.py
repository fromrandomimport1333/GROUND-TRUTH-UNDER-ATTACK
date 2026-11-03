"""Auditable temperature features. Missing evidence stays null/unavailable."""
from collections import defaultdict
import math
import statistics
import numpy as np

from core.contracts import FeatureGroup, FeatureValue, ProcessedReading
from core.processing.policy import ProcessingPolicy


def finite(value):
    return float(value) if value is not None and math.isfinite(value) else None


def stable_mean(values):
    if not values:
        return None
    scale = max(abs(v) for v in values)
    return 0.0 if scale == 0 else finite((math.fsum(v / scale for v in values) / len(values)) * scale)


def stable_median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    return ordered[n//2] if n % 2 else stable_mean(ordered[n//2-1:n//2+1])


def stable_std(values):
    if not values:
        return None
    scale = max(abs(v) for v in values)
    if scale == 0:
        return 0.0
    scaled = [v / scale for v in values]
    mean = math.fsum(scaled) / len(scaled)
    return finite(math.sqrt(math.fsum((v - mean)**2 for v in scaled) / len(scaled)) * scale)


def feature(name, sensor, value, unit=None):
    return FeatureValue(name=name, sensor_id=sensor, value=finite(value), unit=unit)


def grouped(readings):
    result = defaultdict(list)
    for item in readings:
        result[item.provenance.reading.sensor_id].append(item)
    for items in result.values():
        items.sort(key=lambda p: (p.provenance.reading.observed_at, p.provenance.reading.identity))
    return dict(sorted(result.items()))


def contiguous(a: ProcessedReading, b: ProcessedReading) -> bool:
    x, y = a.provenance.reading, b.provenance.reading
    return (a.ready_for_analysis and b.ready_for_analysis and
            x.identity[:3] == y.identity[:3] and y.sequence == x.sequence + 1 and
            y.observed_at > x.observed_at)


def temporal_features(readings, policy: ProcessingPolicy) -> FeatureGroup:
    if not readings:
        return FeatureGroup(availability='unavailable', reason='no_observations')
    output = []
    for sensor, items in grouped(readings).items():
        usable = [p for p in items if p.ready_for_analysis]
        values = [p.normalized_value for p in usable]
        # A rolling segment cannot cross nulls, rejected samples or stream changes.
        trailing = []
        if items[-1].ready_for_analysis:
            trailing = [items[-1]]
            for item in reversed(items[:-1]):
                if len(trailing) >= policy.rolling_samples or not contiguous(item, trailing[0]):
                    break
                trailing.insert(0, item)
        rolling = [p.normalized_value for p in trailing]
        last_pair = len(trailing) >= 2
        delta = finite(rolling[-1] - rolling[-2]) if last_pair else None
        dt = ((trailing[-1].provenance.reading.observed_at - trailing[-2].provenance.reading.observed_at).total_seconds()
              if last_pair else None)
        all_contiguous = len(items) >= 2 and all(contiguous(a, b) for a, b in zip(items, items[1:]))
        intervals = [(b.provenance.reading.observed_at - a.provenance.reading.observed_at).total_seconds()
                     for a, b in zip(items, items[1:]) if contiguous(a, b)]
        interval = statistics.median(intervals) if intervals else None
        spectral_ok = (all_contiguous and len(items) >= policy.spectral_min_samples and interval is not None and
                       all(abs(d - interval) <= policy.spectral_relative_jitter * interval for d in intervals))
        frequency, power_fraction = None, None
        if spectral_ok:
            scale = max(abs(v) for v in values)
            scaled = np.array(values, dtype=float) / (scale or 1.0)
            centered = scaled - scaled.mean()
            power = np.abs(np.fft.rfft(centered)) ** 2
            power[0] = 0.0
            total = float(power.sum())
            if total > 0:
                index = int(np.argmax(power))
                frequency = float(np.fft.rfftfreq(len(values), d=interval)[index])
                power_fraction = float(power[index] / total)
        constant = None
        if len(rolling) >= 2:
            constant = float(max(rolling) - min(rolling) <= policy.constant_tolerance_c)
        # Counts and binary eligibility describe observed conditions, not imputed evidence.
        fields = [
            ('sample_count', len(items), 'count'), ('usable_count', len(values), 'count'),
            ('missing_count', sum(p.provenance.reading.value is None for p in items), 'count'),
            ('excluded_count', len(items)-len(values), 'count'),
            ('mean_c', stable_mean(values), 'C'), ('std_c', stable_std(values), 'C'),
            ('min_c', min(values) if values else None, 'C'), ('max_c', max(values) if values else None, 'C'),
            ('rolling_count', len(rolling), 'count'), ('rolling_mean_c', stable_mean(rolling), 'C'),
            ('rolling_std_c', stable_std(rolling), 'C'), ('last_delta_c', delta, 'C'),
            ('last_dt_seconds', dt, 's'), ('last_rate_c_per_s', finite(delta / dt) if delta is not None else None, 'C/s'),
            ('constant_run', constant, 'boolean'), ('median_interval_seconds', interval, 's'),
            ('spectral_eligible', float(spectral_ok), 'boolean'),
            ('dominant_frequency_hz', frequency, 'Hz'), ('dominant_power_fraction', power_fraction, 'ratio'),
        ]
        output.extend(feature(name, sensor, value, unit) for name, value, unit in fields)
    return FeatureGroup(availability='available', values=tuple(output))


def distance_km(a, b):
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat, dlon = lat2-lat1, math.radians(b.longitude-a.longitude)
    hav = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))


def spatial_features(readings, policy: ProcessingPolicy) -> FeatureGroup:
    if not policy.neighbor_pairs:
        return FeatureGroup(availability='unavailable', reason='neighbors_not_configured')
    groups = grouped(readings)
    output = []
    any_match = False
    for sensor, items in groups.items():
        targets = [p for p in items if p.ready_for_analysis]
        target = targets[-1] if targets else None
        matches = []
        if target is not None and target.provenance.reading.location is not None:
            raw = target.provenance.reading
            for origin, neighbor in policy.neighbor_pairs:
                if origin != sensor:
                    continue
                candidates = [p for p in groups.get(neighbor, ()) if p.ready_for_analysis and
                              p.provenance.reading.observed_at <= raw.observed_at]
                if not candidates:
                    continue
                candidate = candidates[-1]
                peer = candidate.provenance.reading
                age = (raw.observed_at - peer.observed_at).total_seconds()
                if peer.location is not None and age <= policy.spatial_max_age_seconds and distance_km(raw.location, peer.location) <= policy.spatial_max_distance_km:
                    matches.append(candidate.normalized_value)
        consensus = stable_median(matches)
        any_match |= bool(matches)
        output.extend((feature('neighbor_count', sensor, len(matches), 'count'),
                       feature('neighbor_median_c', sensor, consensus, 'C'),
                       feature('neighbor_deviation_c', sensor, target.normalized_value-consensus if matches else None, 'C'),
                       feature('neighbor_std_c', sensor, stable_std(matches), 'C')))
    if not any_match:
        return FeatureGroup(availability='unavailable', reason='no_eligible_neighbors')
    return FeatureGroup(availability='available', values=tuple(output))
