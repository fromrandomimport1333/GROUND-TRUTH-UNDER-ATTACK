"""Opt-in hourly mode for scripts.export_wegenernet; no alternate verifier."""
import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import timedelta
import gzip
import json
from pathlib import Path
import tempfile
import time

from adapters.wegenernet import GridSource, VARIABLES, parse_cells, cell_id
from adapters.feature_export import FeatureExport
from attacks.scenario import DatasetExperiment
from core.processing.engine import ProcessingEngine


def export_hourly(paths,cells,out,start,end,warmup_hours=168):
    source = GridSource(paths,cells,start,end,warmup_hours)
    out = Path(out)
    out.mkdir(parents=True,exist_ok=False)
    ids = [cell_id(c) for c in source.cells]
    engine = ProcessingEngine.hourly(ids,source.neighbors,VARIABLES)
    manifest = dict(format_version='hourly-1',cells=ids,neighbors=source.neighbors,grid=source.grid,
        variables=VARIABLES,sources=[d for _,d in source.files],
        interval_start=source.start.isoformat(),interval_end=source.end.isoformat(),
        requested_warmup_hours=warmup_hours,available_warmup_hours=source.available_warmup,
        missing_warmup_hours=warmup_hours-source.available_warmup,
        provenance='Public reproducible OFFLINE EXPERIMENT signatures, not original sensor authentication.',
        availability='Simulated at end-of-hour, not actual publication time.',
        window='FeatureWindow [label,label+1h); label is source interval end. History is linked in lineage.jsonl.gz.',
        flags='Raw percentages, >0 count is descriptive only. No threshold-based quarantine.',
        references='No independent satellite/reanalysis reference supplied or implemented here.')
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    (out/'FeatureWindow.schema.json').write_text(json.dumps(__import__('core.contracts',fromlist=['FeatureWindow']).FeatureWindow.model_json_schema(),indent=2),encoding='utf-8')
    statuses = Counter(); flag_counts = {}; num_hours = audit_count = 0
    started = time.monotonic()
    writer = FeatureExport(out)
    try:
        with tempfile.TemporaryDirectory() as td, gzip.open(out/'observations.jsonl.gz','wt',encoding='utf-8') as journal:
            session = DatasetExperiment(Path(td),[cell_id(c) for c in source.input_cells],
                                        'wegenernet_v8_hourly_public_fixture')
            (out/'experiment-public-keys.json').write_text(json.dumps(session.enrollment,indent=2),encoding='utf-8')
            for timestamp,samples,evidence in source.hours():
                events=[]
                for sample,meta in zip(samples,evidence,strict=True):
                    eid,event,payload = session.verify(sample)
                    events.append(event)
                    # Keep exact wire bytes, full verifier output and source coordinates once.
                    journal.write(json.dumps({'source':meta,'event_id':eid,'event':event.model_dump(mode='json'),
                                              'wire_cbor_hex':payload.hex()},separators=(',',':'))+'\n')
                    session.verifier.acknowledge(eid)
                    statuses[event.status] += 1
                    audit_count += 1
                    if sample.sensor_id in ids and source.start < timestamp <= source.end:
                        counts = flag_counts.setdefault(sample.quantity,Counter())
                        value = meta['flag_percent']
                        counts['total']+=1
                        counts['missing' if value is None else 'nonzero' if value>0 else 'zero']+=1
                        if value is not None:
                            counts['min'] = min(counts.get('min',value),value)
                            counts['max'] = max(counts.get('max',value),value)
                results = engine.process(timestamp,events,evidence)
                if source.start < timestamp <= source.end:
                    for window,row,lineage in results:
                        writer.write(window,row,lineage)
                    num_hours += 1
                    if num_hours%24 == 0:
                        print(f'{num_hours} hours; {writer.rows} cell-hours; {audit_count} verified records',flush=True)
    finally:
        writer.close()
    expected = int((source.end-source.start).total_seconds()/3600)
    if writer.rows != expected*len(ids) or num_hours != expected:
        raise RuntimeError('Output cardinality mismatch')
    summary = dict(hours=num_hours,cell_hours=writer.rows,cells=len(ids),input_cells=len(source.input_cells),
                   audited_observations=audit_count,statuses=dict(statuses),flags=flag_counts,
                   missing_features=writer.missing,full_jsonl_uncompressed_bytes=writer.full_bytes,
                   sizes_bytes={p.name:p.stat().st_size for p in out.iterdir() if p.is_file() and not p.name.startswith('.')},
                   elapsed_seconds=time.monotonic()-started,detection_evaluated=False,
                   full_year_validated=False,available_warmup_hours=source.available_warmup)
    (out/'SUCCESS.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    return summary


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('files',nargs='+');p.add_argument('--cells-file',required=True)
    p.add_argument('--output',required=True);p.add_argument('--start',required=True);p.add_argument('--end',required=True)
    p.add_argument('--warmup-hours',type=int,default=168)
    a=p.parse_args(argv)
    print(json.dumps(export_hourly(a.files,parse_cells(a.cells_file),a.output,a.start,a.end,a.warmup_hours),indent=2))


if __name__=='__main__':
    main()
