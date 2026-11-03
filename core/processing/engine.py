"""Pure bounded window construction from internal verifier events."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
from itertools import islice
import json

from core.contracts import FeatureWindow, ProcessedReading, ReferenceObservation, VerifiedReading
from core.processing.alignment import window_bounds
from core.processing.features import spatial_features, temporal_features
from core.processing.policy import ProcessingPolicy
from core.processing.references import ReferenceMatch, reference_features
from core.processing.validator import validate_reading
from core.provenance.replay_protection import require_utc


@dataclass(frozen=True)
class ExcludedEvent:
    reason: str
    event: VerifiedReading


@dataclass(frozen=True)
class ExcludedReference:
    reason: str
    reference: ReferenceObservation


@dataclass(frozen=True)
class WindowResult:
    window: FeatureWindow
    excluded_events: tuple[ExcludedEvent, ...]
    excluded_references: tuple[ExcludedReference, ...]
    policy: ProcessingPolicy
    reference_matches: tuple[ReferenceMatch, ...]


def bounded(values, maximum, label):
    result = list(islice(iter(values), maximum + 1))
    if len(result) > maximum:
        raise BufferError(f'{label} exceeds configured bound; no partial window emitted')
    return result


class ProcessingEngine:
    """No hidden mutable history, imputation, clock shifts or dataset coercion.

    Pass internal VerifiedReading objects from the trusted L1b boundary. The
    class validates structure but cannot authenticate a fabricated verdict.
    """
    def __init__(self, policy: ProcessingPolicy | None = None):
        self.policy = ProcessingPolicy.model_validate(policy or ProcessingPolicy())

    def build_window(self, events, *, start: datetime, end: datetime,
                     as_of: datetime, references=()) -> WindowResult:
        start, end, as_of = map(require_utc, (start, end, as_of))
        if window_bounds(start, self.policy.window_seconds) != (start, end):
            raise ValueError('start/end must form one policy-aligned UTC window')
        if as_of < end:
            raise ValueError('as_of must be at or after window end')
        originals = bounded(events, self.policy.max_events, 'event batch')
        groups, excluded = defaultdict(list), []
        for event in originals:
            if not isinstance(event, VerifiedReading):
                raise TypeError('only internal VerifiedReading instances are accepted')
            event = VerifiedReading.model_validate(event)
            if event.reading is None:
                excluded.append(ExcludedEvent('malformed_no_event_time', event))
            elif event.checked_at > as_of:
                excluded.append(ExcludedEvent('not_available_as_of', event))
            elif not start <= event.reading.observed_at < end:
                excluded.append(ExcludedEvent('outside_window', event))
            else:
                groups[event.reading.identity].append(event)
        if len({identity[0] for identity in groups}) > self.policy.max_sensors:
            raise BufferError('sensor count exceeds configured bound')
        if len(groups) > 10000:
            raise BufferError('unique readings exceed FeatureWindow contract capacity')
        processed = []
        for identity in sorted(groups):
            candidates = groups[identity]
            # A rejected forged copy cannot displace an authenticated observation.
            candidates.sort(key=lambda e: (e.status != 'valid', e.checked_at, e.model_dump_json()))
            chosen = candidates[0]
            valid_versions = {e.reading.model_dump_json() for e in candidates if e.status == 'valid'}
            conflict = len(valid_versions) > 1
            item = validate_reading(chosen)
            if conflict:
                item = ProcessedReading(provenance=chosen,
                    quality_flags=(*item.quality_flags, 'conflicting_verified_identity'))
            for other in candidates[1:]:
                reason = 'duplicate_identity' if other.reading == chosen.reading else 'conflicting_identity'
                excluded.append(ExcludedEvent(reason, other))
            processed.append(item)
        processed.sort(key=lambda p: (p.provenance.reading.observed_at, p.provenance.reading.identity))
        refs, ref_excluded = [], []
        for ref in bounded(references, self.policy.max_references, 'reference batch'):
            if not isinstance(ref, ReferenceObservation):
                raise TypeError('reference adapters must emit ReferenceObservation instances')
            ref = ReferenceObservation.model_validate(ref)
            if ref.available_at > as_of:
                ref_excluded.append(ExcludedReference('not_available_as_of', ref))
            elif ref.observed_at >= end:
                ref_excluded.append(ExcludedReference('reference_at_or_after_end', ref))
            else:
                refs.append(ref)
        # Preserve source evidence, but remove identical repeats deterministically.
        unique_refs = set()
        for ref in refs:
            if ref in unique_refs:
                ref_excluded.append(ExcludedReference("duplicate_reference", ref))
            unique_refs.add(ref)
        refs = sorted(unique_refs, key=lambda r: r.model_dump_json())
        matches = []
        reference_group = reference_features(processed, refs, self.policy, matches=matches)
        data = dict(schema_version='1.0', start=start, end=end, as_of=as_of,
                    readings=tuple(processed), temporal_features=temporal_features(processed, self.policy),
                    spatial_features=spatial_features(processed, self.policy),
                    reference_features=reference_group, reference_data=tuple(refs))
        provisional = FeatureWindow(window_id='pending', **data)
        fingerprint = json.dumps({'policy': self.policy.model_dump(mode='json'),
                                  'window': provisional.model_dump(mode='json', exclude={'window_id'})},
                                 sort_keys=True, separators=(',', ':'), allow_nan=False)
        window_id = 'window_' + hashlib.sha256(fingerprint.encode()).hexdigest()[:40]
        return WindowResult(FeatureWindow(window_id=window_id, **data), tuple(excluded), tuple(ref_excluded), self.policy, tuple(matches))

    def build_closed_windows(self, events, *, as_of: datetime, references=()):
        """Partition a bounded batch by UTC event time; emit only complete windows.

        Returns (window results, unassigned evidence). No empty intervals or
        synthetic measurements are generated. Supply the next batch explicitly.
        """
        as_of = require_utc(as_of)
        groups, unassigned = defaultdict(list), []
        refs = bounded(references, self.policy.max_references, 'reference batch')
        for event in bounded(events, self.policy.max_events, 'event batch'):
            if not isinstance(event, VerifiedReading):
                raise TypeError('only internal VerifiedReading instances are accepted')
            event = VerifiedReading.model_validate(event)
            if event.reading is None:
                unassigned.append(ExcludedEvent('malformed_no_event_time', event))
            elif event.checked_at > as_of:
                unassigned.append(ExcludedEvent('not_available_as_of', event))
            else:
                bounds = window_bounds(event.reading.observed_at, self.policy.window_seconds)
                if bounds[1] > as_of:
                    unassigned.append(ExcludedEvent('window_not_closed', event))
                else:
                    groups[bounds].append(event)
        if len(groups) > 128:
            raise BufferError('at most 128 windows per bounded batch')
        results = tuple(self.build_window(items, start=start, end=end, as_of=as_of, references=refs)
                        for (start, end), items in sorted(groups.items()))
        return results, tuple(unassigned)


    @staticmethod
    def hourly(cells, neighbors, variables):
        """Opt-in multivariate hourly processor; legacy windows remain unchanged."""
        from core.processing.hourly import HourlyEngine
        return HourlyEngine(cells, neighbors, variables)
