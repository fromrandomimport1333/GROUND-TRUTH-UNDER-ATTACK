import argparse,json,tempfile
from datetime import timedelta
from pathlib import Path
from adapters.wegenernet import hours
from attacks.scenario import sign_experiment
from adapters.replay import ReplayAdapter
from core.processing.policy import ProcessingPolicy
from core.provenance.keyregistry import KeyRegistry

def export(paths,cells,out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    ids=[f'wn_grid_y{y}_x{x}' for y,x in cells]
    policy=ProcessingPolicy(window_seconds=86400,rolling_samples=24,spatial_max_age_seconds=0.0,neighbor_pairs=tuple((a,b) for a in ids for b in ids if a!=b))
    manifest=dict(provenance='Experimental signatures per UTC day, NOT WegenerNet sensor authentication.',grid='Derived correlated virtual sensors; not independent physical stations.',quality='Flagged_T preserved separately; no filtering or imputation.',time='End-of-hour timestamps, simulated availability at interval end; NOT historical publication availability.',window='Daily UTC [start,end); edge windows can be partial. No history across windows.',policy=policy.model_dump(mode='json'))
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    count=total=0
    def flush(samples,evidence):
        nonlocal count,total
        if not samples:return
        end=samples[0].observed_at.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);rec=sign_experiment(samples,root/'signers');reg=KeyRegistry.create(root/'registry.sqlite3');rec.enroll(reg)
            windows=[]
            result=ReplayAdapter(reg,layer4=lambda *a:None,layer3=windows.append,processing_policy=policy).run(rec.records,as_of=end)
            if len(windows)!=1 or any(e.status!='valid' for e in result.events):raise RuntimeError('Replay failed')
            w=windows[0];wf.write(w.model_dump_json()+'\n')
            ef.write(json.dumps(dict(window_id=w.window_id,complete_24_hours=len(samples)==24*len(cells),records=evidence))+'\n')
            count+=1;total+=len(samples)
    with (out/'windows.jsonl').open('w',encoding='utf-8') as wf,(out/'evidence.jsonl').open('w',encoding='utf-8') as ef:
        day=None;samples=[];evidence=[]
        for dt,batch,source in hours(paths,cells):
            if day is not None and dt.date()!=day:
                flush(samples,evidence);samples=[];evidence=[]
            day=dt.date();samples.extend(batch);evidence.extend(source)
        flush(samples,evidence)
    summary=dict(windows=count,readings=total,detection_evaluated=False)
    (out/'SUCCESS.json').write_text(json.dumps(summary),encoding='utf-8')
    return summary
def main():
    import sys
    if '--cells-file' in sys.argv:
        from scripts.export_wegenernet_hourly import main as hourly_main
        return hourly_main()

    p=argparse.ArgumentParser();p.add_argument('files',nargs='+');p.add_argument('--output',required=True);p.add_argument('--cells',default='80:115,80:116,80:117')
    a=p.parse_args();print(export(a.files,[tuple(map(int,s.split(':'))) for s in a.cells.split(',')],a.output))
if __name__=='__main__':main()
