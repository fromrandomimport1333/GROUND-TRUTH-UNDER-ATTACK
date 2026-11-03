"""Dataset-independent causal reference matching; no external source is invented."""
from dataclasses import dataclass
from core.contracts import FeatureGroup, ReferenceObservation
from core.processing.features import distance_km, feature, grouped
from core.processing.validator import normalize_temperature


@dataclass(frozen=True)
class ReferenceMatch:
    sensor_id: str
    reference: ReferenceObservation


def reference_features(readings, references, policy, *, matches=None) -> FeatureGroup:
    if not policy.reference_sources:
        return FeatureGroup(availability='unavailable', reason='reference_sources_not_configured')
    output, any_match = [], False
    for sensor, items in grouped(readings).items():
        targets = [p for p in items if p.ready_for_analysis]
        target = targets[-1] if targets else None
        candidates = []
        if target is not None and target.provenance.reading.location is not None:
            raw = target.provenance.reading
            for reference in references:
                if reference.source_id not in policy.reference_sources or reference.location is None:
                    continue
                # References observed after the sensor event cannot explain that event causally.
                age = (raw.observed_at-reference.observed_at).total_seconds()
                if not 0 <= age <= policy.reference_max_age_seconds:
                    continue
                distance = distance_km(raw.location, reference.location)
                if distance > policy.reference_max_distance_km:
                    continue
                normalized = normalize_temperature(reference.value, reference.unit, reference.quantity)
                if normalized.value_c is None:
                    continue
                # Newest event, nearest site, newest available revision, then stable ID.
                rank = (-reference.observed_at.timestamp(), distance,
                        -reference.available_at.timestamp(), reference.source_id)
                candidates.append((rank, normalized.value_c, age, distance, reference))
        if candidates:
            candidates.sort(key=lambda c: c[0])
            best = candidates[0]
            # Equally ranked contradictory evidence is ambiguous, never input-order selected.
            tied = [c for c in candidates if c[0] == best[0]]
            if len({c[1] for c in tied}) != 1:
                best = None
        else:
            best = None
        any_match |= best is not None
        value, age, distance = best[1:4] if best else (None, None, None)
        if best is not None and matches is not None:
            matches.append(ReferenceMatch(sensor, best[4]))
        output.extend((feature('reference_c', sensor, value, 'C'),
                       feature('reference_deviation_c', sensor, target.normalized_value-value if best else None, 'C'),
                       feature('reference_age_seconds', sensor, age, 's'),
                       feature('reference_distance_km', sensor, distance, 'km')))
    if not any_match:
        return FeatureGroup(availability='unavailable', reason='no_unambiguous_reference_match')
    return FeatureGroup(availability='available', values=tuple(output))
