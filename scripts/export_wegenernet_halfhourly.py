"""Monthly half-hour exports using the existing L1/L2 pipeline.

Resume deterministically replays earlier inputs (including warm-up), retaining
completed immutable partitions. Only complete partitions have SUCCESS.json.
"""
import argparse
from collections import Counter
from datetime import datetime,timedelta,timezone
import gzip
import json
from pathlib import Path
import shutil
import tempfile
import time

from adapters.wegenernet import GridSource, parse_cells, cell_id, file_digest
from adapters.feature_export import FeatureExport
from attacks.scenario import DatasetExperiment
from core.processing.hourly import HourlyEngine
from core.contracts import FeatureWindow


def export(paths,cells,out,start,end,warmup_hours=168,resume=False,*,offline_memory=True):
    source=GridSource(paths,cells,start,end,warmup_hours,interval_seconds=1800)
    out=Path(out)
    ids=[cell_id(c) for c in source.cells]
    identity=dict(interval_start=source.start.isoformat(),interval_end=source.end.isoformat(),
        cells=ids,interval_seconds=1800,requested_warmup_hours=warmup_hours,
        source_hashes=[d['sha256'] for _,d in source.files])
    if out.exists():
        if not resume: raise FileExistsError('Use a new output directory or --resume')
        if json.loads((out/'run-identity.json').read_text())!=identity:
            raise ValueError('Resume configuration or source bytes changed')
    else:
        out.mkdir(parents=True)
        (out/'run-identity.json').write_text(json.dumps(identity,indent=2))
    manifest=dict(**identity,format_version='half-hourly-1',neighbors=source.neighbors,
        grid=source.grid,variables=source.variables,sources=[d for _,d in source.files],
        available_warmup_samples=source.available_warmup,
        missing_warmup_samples=warmup_hours*2-source.available_warmup,
        provenance='Public reproducible OFFLINE EXPERIMENT signatures; not source authentication.',
        window='[source end label,label+30min); as_of=label+30min. Offline modeled availability.',
        history='Across monthly partitions; all observation journals and warmup journal must be retained.',
        references='Independent cross-source reference unavailable',
        flags='Raw percentages; >0 descriptive only, no quarantine threshold',
        resume='Replays from first available warmup; verifies completed partition hashes; rebuilds incomplete partition.')
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (out/'SUCCESS.json').unlink(missing_ok=True)
    completed={}
    for p in sorted(out.glob('????-??/SUCCESS.json')):
        entry=json.loads(p.read_text())
        for name,digest in entry['sha256'].items():
            if file_digest(p.parent/name)!=digest: raise ValueError(f'Completed partition changed: {p.parent/name}')
        completed[p.parent.name]=entry
    engine=HourlyEngine(ids,source.neighbors,source.variables,interval_seconds=1800)
    started=time.monotonic(); current=None;writer=journal=None;directory=None;skip=False
    stats=None;partitions=dict(completed)
    def close_partition():
        nonlocal writer,journal
        if writer is None:return
        writer.close();journal.close()
        year,month=map(int,current.split('-'))
        first=datetime(year,month,1,tzinfo=timezone.utc)
        following=datetime(year+int(month==12),1 if month==12 else month+1,1,tzinfo=timezone.utc)
        left,right=max(first,source.start),min(following,source.end)
        required=int((right-left).total_seconds()/1800)
        if stats['intervals']!=required or writer.rows!=required*len(ids):
            raise RuntimeError('Incomplete monthly partition')
        stats.update(interval_start=left.isoformat(),interval_end=right.isoformat())
        stats.update(rows=writer.rows,missing_features=writer.missing,
                     full_jsonl_uncompressed_bytes=writer.full_bytes)
        stats['sizes_bytes']={p.name:p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.name.startswith('.')}
        stats['sha256']={name:file_digest(directory/name) for name in stats['sizes_bytes']}
        temp=directory/'SUCCESS.tmp';temp.write_text(json.dumps(stats,indent=2));temp.replace(directory/'SUCCESS.json')
        partitions[current]=stats;writer=journal=None
    session=None
    try:
        with tempfile.TemporaryDirectory(dir=out.parent, prefix=".offline-state-") as td, gzip.open(out/'warmup-observations.jsonl.gz','wt',compresslevel=1,encoding='utf-8') as warm:
            session=DatasetExperiment(Path(td),[cell_id(c) for c in source.input_cells], 'wegenernet_v8_halfhourly_public_fixture', offline_memory=offline_memory)
            (out/'experiment-public-keys.json').write_text(json.dumps(session.enrollment,indent=2))
            for timestamp,samples,evidence in source.hours():
                target=source.start<timestamp<=source.end
                month=(timestamp-timedelta(seconds=1800)).strftime('%Y-%m')
                if target and month!=current:
                    close_partition();current=month;skip=month in completed
                    directory=out/month
                    if not skip:
                        if directory.exists():shutil.rmtree(directory)
                        directory.mkdir()
                        writer=FeatureExport(directory,compresslevel=1)
                        journal=gzip.open(directory/'observations.jsonl.gz','wt',compresslevel=1,encoding='utf-8')
                        stats=dict(intervals=0,audited_observations=0,statuses={},flags={})
                        (directory/'FeatureWindow.schema.json').write_text(json.dumps(FeatureWindow.model_json_schema()))
                events=[]
                for sample,meta in zip(samples,evidence,strict=True):
                    eid,event,payload=session.verify(sample);events.append(event)
                    sink=journal if target and not skip else warm if not target else None
                    if sink is not None:
                        sink.write(json.dumps(dict(source=meta,event_id=eid,event=event.model_dump(mode='json'),wire_cbor_hex=payload.hex()),separators=(',',':'))+'\n')
                    session.verifier.acknowledge(eid)
                    if target and not skip:
                        stats['audited_observations']+=1
                        stats['statuses'][event.status]=stats['statuses'].get(event.status,0)+1
                        if sample.sensor_id in ids:
                            flags=stats['flags'].setdefault(sample.quantity,dict(total=0,missing=0,zero=0,nonzero=0))
                            flags['total']+=1;v=meta['flag_percent']
                            flags['missing' if v is None else 'nonzero' if v>0 else 'zero']+=1
                session.discard_delivered()
                if target and skip and (timestamp.hour == 0 and timestamp.minute == 0):
                    print(f'{current}: replaying completed partition; {timestamp.isoformat()}',flush=True)
                results=engine.process(timestamp,events,evidence)
                if target and not skip:
                    for window,row,lineage in results:writer.write(window,row,lineage)
                    stats['intervals']+=1
                    if stats['intervals']%48==0:print(f'{current}: {stats["intervals"]} intervals; {writer.rows} rows',flush=True)
            close_partition()
    finally:
        if session is not None:session.close()
        if writer is not None:writer.close()
        if journal is not None:journal.close()
    expected=int((source.end-source.start).total_seconds()/1800)*len(ids)
    rows=sum(p['rows'] for p in partitions.values())
    if rows!=expected:raise RuntimeError('Cardinality mismatch')
    annual={}
    for month,part in sorted(partitions.items()):
        year=month[:4]
        entry=annual.setdefault(year,dict(intervals=0,rows=0,months=[]))
        entry['intervals']+=part['intervals'];entry['rows']+=part['rows'];entry['months'].append(month)
    result=dict(rows=rows,expected_rows=expected,annual_totals=annual,partitions=partitions,elapsed_seconds=time.monotonic()-started,
                supplied_period_complete=True,full_2023_2024_validated=False,available_warmup_samples=source.available_warmup)
    (out/'SUCCESS.json').write_text(json.dumps(result,indent=2))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('files',nargs='+');p.add_argument('--cells-file',required=True)
    p.add_argument('--output',required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--warmup-hours',type=int,default=168);p.add_argument('--resume',action='store_true')
    p.add_argument("--disk-state",action="store_true",help="Use original temporary disk state for comparison")
    a=p.parse_args();print(json.dumps(export(a.files,parse_cells(a.cells_file),a.output,a.start,a.end,a.warmup_hours,a.resume,offline_memory=not a.disk_state),indent=2))

if __name__=='__main__':main()
