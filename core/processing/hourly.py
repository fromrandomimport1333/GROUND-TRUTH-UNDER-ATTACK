"""Hourly multivariate extension; only eligible verified values enter history."""
from collections import defaultdict, deque
from datetime import timedelta
import hashlib
import json

from core.contracts import FeatureWindow, FeatureGroup
from core.processing.features import feature, stable_mean, stable_std, stable_median
from core.processing.validator import validate_reading


def temporal(values, run_length, run_censored, steps_per_hour=1):
    """Current included. N differences require N+1 consecutive hourly values."""
    result = {'latest': values[-1], 'history_count': float(len(values)),
              'run_length_identical': float(run_length) if values[-1] is not None else None,
              'run_length_left_censored': float(run_censored) if values[-1] is not None else None}
    def tail(n):
        part = values[-n:]
        return part if len(part) == n and all(v is not None for v in part) else None
    for n in (3,6):
        part = tail(n*steps_per_hour)
        result[f'std_{n}h'] = stable_std(part) if part else None
    for n in (24,168):
        part = tail(n*steps_per_hour)
        result[f'mean_{n}h'] = stable_mean(part) if part else None
    part = tail(steps_per_hour+1)
    result['delta_1h'] = part[-1]-part[0] if part else None
    if steps_per_hour == 2:
        part = tail(2)
        result['delta_30min'] = part[-1]-part[0] if part else None
    for n in (6,24):
        part = tail(n*steps_per_hour+1)
        result[f'zero_diff_count_{n}h'] = float(sum(a == b for a,b in zip(part,part[1:]))) if part else None
    part = tail(6*steps_per_hour+1)
    result['max_abs_delta_6h'] = max(abs(a-b) for a,b in zip(part,part[1:])) if part else None
    return result


def spatial(center, neighbors):
    valid = [v for v in neighbors.values() if v is not None]
    result = {'neighbor_count': float(len(valid))}
    result['minus_neighbor_mean'] = center-stable_mean(valid) if center is not None and valid else None
    result['minus_neighbor_median_4'] = center-stable_median(valid) if center is not None and len(valid)>=4 else None
    for direction in ('ym','yp','xm','xp'):
        peer = neighbors.get(direction)
        result[f'minus_neighbor_{direction}'] = center-peer if center is not None and peer is not None else None
    return result


def feature_unit(name, unit):
    if name.endswith(('count','censored')) or name.startswith(('zero_diff_count','run_length','history_count','neighbor_count','flagged_observation_count')):
        return 'count' if not name.endswith('censored') else 'boolean'
    return unit


class HourlyEngine:
    """Bounded 169-hour history plus O(series) running identical counters.

    Ingest ALL verifier verdicts, not the eligible-only callback. Missing hours
    reset history, rejected/null samples break runs. Never fall back to stale
    spatial samples. Raw source flags are quality metadata, not L1 verdicts.
    """
    def __init__(self, cells, neighbors, variables, interval_seconds=3600):
        if interval_seconds not in (1800,3600):
            raise ValueError("Unsupported cadence")
        self.interval_seconds = interval_seconds
        self.steps_per_hour = 3600 // interval_seconds
        self.cells, self.neighbors, self.variables = sorted(cells), neighbors, variables
        self.history = defaultdict(lambda: deque(maxlen=168*self.steps_per_hour+1))
        self.runs = {}
        self.run_starts = {}
        self.last_time = None

    def process(self, timestamp, events, evidence):
        if self.last_time is not None and timestamp <= self.last_time:
            raise ValueError('Hours must increase; replay events belong in their current hour')
        contiguous = self.last_time is not None and timestamp-self.last_time == timedelta(seconds=self.interval_seconds)
        if not contiguous:
            self.history.clear()
            self.runs.clear()
            self.run_starts.clear()
        self.last_time = timestamp
        metadata = {}
        processed = {}
        # One input per sensor/quantity/hour; reject ambiguities rather than overwrite.
        for event, source in zip(events, evidence, strict=True):
            if event.reading is None:
                continue  # remains in the all-verdict audit journal
            raw = event.reading
            if raw.observed_at != timestamp:
                raise ValueError('Event timestamp does not match hourly bucket')
            key = raw.sensor_id, raw.quantity
            if key in processed:
                raise ValueError('Duplicate sensor/quantity/hour; resolve through audit before processing')
            processed[key] = validate_reading(event)
            metadata[key] = source
        for sid in self.cells:
            for name in self.variables:
                key = sid,name
                item = processed.get(key)
                value = item.normalized_value if item and item.ready_for_analysis else None
                hist = self.history[key]
                previous = hist[-1][0] if hist else None
                old_run, old_censored = self.runs.get(key,(0,True))
                if value is None:
                    run,censored = 0,True
                elif previous is not None and value == previous:
                    run,censored = old_run+1,old_censored
                else:
                    run,censored = 1,previous is None
                if run <= 1:
                    self.run_starts[key] = metadata.get(key,{}).get('observation_id') if run else None
                self.runs[key] = run,censored
                hist.append((value, metadata.get(key,{}).get('observation_id')))
        results = []
        for sid in self.cells:
            tf,sf = [],[]
            lineage = {'timestamp':timestamp.isoformat(),'cell_id':sid,'observations':{},'history':{},'neighbors':{},'run_start_ids':{},'interval_seconds':self.interval_seconds}
            row = {'timestamp':timestamp.isoformat(),'cell_id':sid}
            included = []
            wanted = {sid,*self.neighbors[sid].values()}
            for key,item in sorted(processed.items()):
                if key[0] in wanted:
                    included.append(item)
            for name,(unit,flag) in self.variables.items():
                key = sid,name
                hist = self.history[key]
                vals = [v for v,_ in hist]
                fields = temporal(vals,*self.runs[key],self.steps_per_hour)
                item = processed.get(key)
                meta = metadata.get(key,{})
                row[name+'__status'] = item.provenance.status if item else 'missing'
                row[name+'__eligible'] = bool(item and item.ready_for_analysis)
                row[name+'__quality_flags'] = '|'.join(item.quality_flags) if item else 'missing_observation'
                row[name+'__observation_id'] = meta.get('observation_id')
                row[name+'__raw_flag_percent'] = meta.get('flag_percent')
                fields['flagged_observation_count'] = (float(meta['flag_percent']>0) if meta.get('flag_percent') is not None else None)
                for fname,value in fields.items():
                    tf.append(feature(name+'__'+fname,sid,value,feature_unit(fname,unit)))
                peers = {}
                neighbor_ids = {}
                for direction,peer in self.neighbors[sid].items():
                    p = processed.get((peer,name))
                    peers[direction] = p.normalized_value if p and p.ready_for_analysis else None
                    neighbor_ids[direction] = metadata.get((peer,name),{}).get('observation_id')
                for fname,value in spatial(vals[-1],peers).items():
                    sf.append(feature(name+'__'+fname,sid,value,feature_unit(fname,unit)))
                lineage['observations'][name] = meta.get('observation_id')
                lineage['run_start_ids'][name] = self.run_starts[key]
                lineage['history'][name] = [oid for _,oid in hist]
                lineage['neighbors'][name] = neighbor_ids
            lo = row_value(processed,sid,'T_zAvgTerr_HMin'.replace('_HM','_HHM') if self.steps_per_hour==2 else 'T_zAvgTerr_HMin')
            hi = row_value(processed,sid,'T_zAvgTerr_HMax'.replace('_HM','_HHM') if self.steps_per_hour==2 else 'T_zAvgTerr_HMax')
            mean = row_value(processed,sid,'T_zAvgTerr_HM'.replace('_HM','_HHM') if self.steps_per_hour==2 else 'T_zAvgTerr_HM')
            tf.append(feature('temperature_extrema_range',sid,hi-lo if hi is not None and lo is not None else None,'K'))
            tf.append(feature('temperature_consistent',sid,float(lo<=mean<=hi) if all(v is not None for v in (lo,mean,hi)) else None,'boolean'))
            data = dict(schema_version='1.0', start=timestamp,end=timestamp+timedelta(seconds=self.interval_seconds),
                        as_of=timestamp+timedelta(seconds=self.interval_seconds), readings=tuple(included),
                        temporal_features=FeatureGroup(availability='available',values=tuple(tf)),
                        spatial_features=FeatureGroup(availability='available',values=tuple(sf)),
                        reference_features=FeatureGroup(availability='unavailable',reason='independent_source_not_supplied'),reference_data=())
            # FeatureWindow contains current envelopes; complete historical input IDs are in lineage.
            fingerprint = json.dumps({'cell':sid,'time':timestamp.isoformat(),'lineage':lineage,
                                       'values':[(v.name,v.value) for v in (*tf,*sf)],
                                       'readings':[p.model_dump(mode='json') for p in included]},sort_keys=True,allow_nan=False)
            wid = ('halfhourly_' if self.steps_per_hour==2 else 'hourly_')+hashlib.sha256(fingerprint.encode()).hexdigest()[:48]
            window = FeatureWindow(window_id=wid,**data)
            row['window_id'] = wid
            for value in (*tf,*sf):
                row[value.name] = value.value
            lineage['window_id'] = wid
            results.append((window,row,lineage))
        return results


def row_value(processed,sid,name):
    item = processed.get((sid,name))
    return item.normalized_value if item and item.ready_for_analysis else None
