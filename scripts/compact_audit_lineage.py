"""Losslessly normalize older explicit-ID lineage to compact journal range references."""
import argparse
import gzip
import json
from pathlib import Path
from adapters.feature_export import compact_lineage


def compact(directory):
    directory=Path(directory);path=directory/'lineage.jsonl.gz';temporary=directory/'lineage.normalized.jsonl.gz'
    old=path.stat().st_size;rows=0
    with gzip.open(path,'rt',encoding='utf-8') as src,gzip.open(temporary,'wt',encoding='utf-8') as dst:
        for line in src:
            dst.write(json.dumps(compact_lineage(json.loads(line)),separators=(',',':'))+'\n');rows+=1
    temporary.replace(path)
    success=directory/'SUCCESS.json'
    if success.exists():
        data=json.loads(success.read_text());data['sizes_bytes']['lineage.jsonl.gz']=path.stat().st_size
        success.write_text(json.dumps(data,indent=2),encoding='utf-8')
    return dict(rows=rows,previous_bytes=old,normalized_bytes=path.stat().st_size)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output');a=p.parse_args();print(json.dumps(compact(a.output),indent=2))
