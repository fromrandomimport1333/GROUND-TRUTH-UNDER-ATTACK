"""Independent NumPy/source validation of a completed clean NetCDF import.

This validator intentionally does not call the feature calculation functions.
It is for clean exports, not attack experiments with intentionally changed inputs.
"""
import argparse
from collections import Counter,deque
import csv
from datetime import datetime,timedelta
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
from netCDF4 import Dataset,num2date
from core.contracts import FeatureWindow


def validate(directory, paths):
    out=Path(directory)
    manifest=json.loads((out/'manifest.json').read_text())
    names=list(manifest['variables'])
    seconds=manifest.get('interval_seconds',3600);steps=3600//seconds
    def records(filename):
        files=sorted(out.glob('????-??/'+filename)) if seconds==1800 else [out/filename]
        if filename=='observations.jsonl.gz' and seconds==1800:
            files=[out/'warmup-observations.jsonl.gz',*files]
        for file in files:
            with gzip.open(file,'rt',encoding='utf-8') as stream:
                yield from stream
    start=datetime.fromisoformat(manifest['interval_start'])
    end=datetime.fromisoformat(manifest['interval_end'])
    cells=sorted(manifest['cells']);neighbors=manifest['neighbors']
    def coordinates(s):
        y,x=s.removeprefix('wn_grid_y').split('_x');return int(y),int(x)
    ids=set(cells)|{s for mapping in neighbors.values() for s in mapping.values()}
    history={};runs={};run_censored={};checks=rows=0
    def equal(actual,expected,label):
        nonlocal checks
        checks+=1
        assert (actual is None and expected is None) or (actual is not None and expected is not None and
            np.isclose(actual,expected,rtol=1e-12,atol=1e-10)),(label,actual,expected)
    def number(v):return None if np.ma.is_masked(v) or not np.isfinite(v) else float(v)
    paths_by_time=[]
    supplied_digests=set()
    for path in paths:
        with Path(path).open('rb') as stream:supplied_digests.add(hashlib.file_digest(stream,'sha256').hexdigest())
    assert supplied_digests=={s['sha256'] for s in manifest['sources']}
    for path in paths:
        with Dataset(path) as f:
            t=f['time'];d=num2date(t[0],t.units,calendar=t.calendar,only_use_cftime_datetimes=False)
            paths_by_time.append((datetime(d.year,d.month,d.day,d.hour,d.minute,tzinfo=start.tzinfo),path))
    if True:
        def compact_rows():
            files=sorted(out.glob('????-??/compact.csv.gz')) if seconds==1800 else [out/'compact.csv.gz']
            for path in files:
                with gzip.open(path,'rt') as stream:yield from csv.DictReader(stream)
        compact=iter(compact_rows());wf=iter(records('windows.jsonl.gz'));lf=iter(records('lineage.jsonl.gz'))
        for _,path in sorted(paths_by_time):
            with Dataset(path) as f:
                time=f['time']
                for i,dt in enumerate(num2date(time[:],time.units,calendar=time.calendar,only_use_cftime_datetimes=False)):
                    stamp=datetime(dt.year,dt.month,dt.day,dt.hour,dt.minute,tzinfo=start.tzinfo)
                    if not start-timedelta(hours=manifest['requested_warmup_hours']) < stamp <= end:continue
                    arrays={name:f[name][i,:,:] for name in names}
                    values={name:{sid:number(arrays[name][coordinates(sid)]) for sid in ids} for name in names}
                    for sid in cells:
                        expected={}
                        for name in names:
                            key=sid,name;value=values[name][sid]
                            seq=history.setdefault(key,deque(maxlen=168*steps+1))
                            prior=seq[-1] if seq else None
                            if value is None:runs[key]=0;run_censored[key]=True
                            elif prior is not None and prior==value:runs[key]+=1
                            else:runs[key]=1;run_censored[key]=prior is None
                            seq.append(value)
                            seq=list(seq)
                            fields={'latest':value,'history_count':float(len(seq)),
                                    'run_length_identical':float(runs[key]) if value is not None else None,
                                    'run_length_left_censored':float(run_censored[key]) if value is not None else None}
                            for n in (3,6,24,168):
                                segment=seq[-n*steps:]
                                ok=len(segment)==n*steps and None not in segment
                                fields[f'std_{n}h' if n in (3,6) else f'mean_{n}h']=(float(np.std(segment)) if n in (3,6) else float(np.mean(segment))) if ok else None
                            fields['delta_1h']=seq[-1]-seq[-(steps+1)] if len(seq)>=steps+1 and None not in seq[-(steps+1):] else None
                            if steps==2:fields['delta_30min']=seq[-1]-seq[-2] if len(seq)>=2 and None not in seq[-2:] else None
                            for n in (6,24):
                                segment=seq[-(n*steps+1):]
                                ok=len(segment)==n*steps+1 and None not in segment
                                diffs=np.diff(segment) if ok else None
                                fields[f'zero_diff_count_{n}h']=float(np.count_nonzero(diffs==0)) if ok else None
                                if n==6:fields['max_abs_delta_6h']=float(np.abs(diffs).max()) if ok else None
                            peers={direction:values[name][peer] for direction,peer in neighbors[sid].items()}
                            valid=[v for v in peers.values() if v is not None]
                            fields['neighbor_count']=float(len(valid))
                            fields['minus_neighbor_mean']=value-float(np.mean(valid)) if value is not None and valid else None
                            fields['minus_neighbor_median_4']=value-float(np.median(valid)) if value is not None and len(valid)>=4 else None
                            for direction in ('ym','yp','xm','xp'):
                                v=peers.get(direction)
                                fields['minus_neighbor_'+direction]=value-v if v is not None and value is not None else None
                            flag=number(f[manifest['variables'][name][1]][i,*coordinates(sid)])
                            fields['flagged_observation_count']=float(flag>0) if flag is not None else None
                            for k,v in fields.items():expected[name+'__'+k]=v
                            expected[name+'__raw_flag_percent']=flag
                        lo=values['T_zAvgTerr_HMin'.replace('_HM','_HHM') if steps==2 else 'T_zAvgTerr_HMin'][sid];hi=values['T_zAvgTerr_HMax'.replace('_HM','_HHM') if steps==2 else 'T_zAvgTerr_HMax'][sid];mean=values['T_zAvgTerr_HM'.replace('_HM','_HHM') if steps==2 else 'T_zAvgTerr_HM'][sid]
                        expected['temperature_extrema_range']=hi-lo if hi is not None and lo is not None else None
                        expected['temperature_consistent']=float(lo<=mean<=hi) if None not in (lo,mean,hi) else None
                        if not start < stamp <= end:continue
                        row=next(compact);w=FeatureWindow.model_validate_json(next(wf));lineage=json.loads(next(lf))
                        assert row['timestamp']==stamp.isoformat() and row['cell_id']==sid
                        assert w.window_id==row['window_id']==lineage['window_id']
                        assert w.start==stamp and w.end==stamp+timedelta(seconds=seconds)
                        assert not w.reference_data and w.reference_features.availability=='unavailable'
                        for reading in w.readings:
                            raw=reading.provenance.reading
                            equal(raw.value,values[raw.quantity][raw.sensor_id],('raw envelope',raw.identity))
                            assert raw.observed_at==stamp and reading.provenance.status=='valid'
                        for name,value in expected.items():equal(float(row[name]) if row[name] else None,value,(stamp,sid,name))
                        for group in (w.temporal_features,w.spatial_features):
                            for v in group.values:equal(v.value,float(row[v.name]) if row[v.name] else None,v.name)
                        for name in names:
                            assert row[name+'__observation_id']==lineage['observations'][name]
                            assert row[name+'__status']=='valid'
                        rows+=1
        assert next(compact,None) is None and next(wf,None) is None and next(lf,None) is None
    expected_rows=len(cells)*int((end-start).total_seconds()/seconds)
    assert rows==expected_rows
    # Audit correlation and exact source-ID formula; every record is retained once.
    seen=set();lookup={};statuses=Counter();audit_count=0
    if True:
        for line in records('observations.jsonl.gz'):
            record=json.loads(line);s=record['source'];event=record['event'];raw=event['reading']
            text=f"wegenernet-observation-v1|{s['file_sha256']}|{s['variable']}|{s['timestamp']}|{s['y']}|{s['x']}"
            oid=hashlib.sha256(text.encode()).hexdigest()
            assert oid==s['observation_id'] and oid not in seen
            seen.add(oid);lookup[(s['sensor_id'],s['variable'],s['timestamp'])]=oid
            statuses[event['status']]+=1;audit_count+=1
            assert hashlib.sha256(bytes.fromhex(record['wire_cbor_hex'])).hexdigest()==event['raw_message_sha256']
            assert raw['sensor_id']==s['sensor_id'] and raw['quantity']==s['variable'] and raw['value']==s['raw_value']
    if True:
        for line in records('lineage.jsonl.gz'):
            lineage=json.loads(line)
            refs=list(lineage['observations'].values())+list(lineage['run_start_ids'].values())
            for variable,history in lineage['history'].items():
                if isinstance(history,list):
                    refs.extend(history)
                else:
                    stamp=datetime.fromisoformat(lineage['timestamp'])
                    sequence=[lookup.get((lineage['cell_id'],variable,(stamp-timedelta(seconds=seconds*(history['slots']-1-i))).isoformat())) for i in range(history['slots'])]
                    assert sequence[0]==history['first_id'] and sequence[-1]==history['last_id']
                    assert [i for i,v in enumerate(sequence) if v is None]==history['missing_offsets']
                    refs.extend(sequence)
            refs += [v for mapping in lineage['neighbors'].values() for v in mapping.values()]
            assert all(v is None or v in seen for v in refs)
    result={'rows':rows,'expected_rows':expected_rows,'numeric_comparisons':checks,
            'audit_records':audit_count,'unique_observation_ids':len(seen),'statuses':dict(statuses),
            'contract_validation':'every FeatureWindow','source_validation':'all selected cell/timestamp rows and features',
            'interval_seconds':seconds,'interval_start':start.isoformat(),'interval_end':end.isoformat(),
            'full_2023_2024_validated':seconds==1800 and start.isoformat()=='2023-01-01T00:00:00+00:00' and end.isoformat()=='2025-01-01T00:00:00+00:00',
            'exit_status':0}
    (out/'independent-validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('output');p.add_argument('files',nargs='+');a=p.parse_args()
    print(json.dumps(validate(a.output,a.files),indent=2))


if __name__=='__main__':main()
