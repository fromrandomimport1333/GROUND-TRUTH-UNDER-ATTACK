"""Describe actual percentage fields, without imposing a quality threshold."""
import argparse
import json
from pathlib import Path
import numpy as np
from netCDF4 import Dataset
from adapters.wegenernet import parse_cells


def inspect(path,cells):
    result={}
    with Dataset(path) as f:
        for group,selected in [('requested_30',cells),('legacy_3',[(80,115),(80,116),(80,117)])]:
            result[group]={}
            for name in ('Flagged_T','Flagged_RH','Flagged_Precip'):
                v=f[name]
                data=np.ma.concatenate([v[:,y,x] for y,x in selected])
                valid=data.compressed();unique,counts=np.unique(valid,return_counts=True)
                result[group][name]={'dtype':str(v.dtype),'metadata':{a:str(v.getncattr(a)) for a in v.ncattrs()},
                    'total':len(data),'masked':int(np.ma.count_masked(data)),
                    'nonzero_count':int(np.count_nonzero(valid>0)),
                    'nonzero_percent_of_valid':float(100*np.count_nonzero(valid>0)/len(valid)),
                    'min':float(valid.min()),'max':float(valid.max()),
                    'observed_values':[{'value':float(x),'count':int(n)} for x,n in zip(unique,counts)]}
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('file');p.add_argument('--cells-file',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();r=inspect(a.file,parse_cells(a.cells_file));Path(a.output).write_text(json.dumps(r,indent=2),encoding='utf-8')
    for group,fields in r.items():
        print(group,{name:{k:v for k,v in values.items() if k not in ('metadata','observed_values')} for name,values in fields.items()})
