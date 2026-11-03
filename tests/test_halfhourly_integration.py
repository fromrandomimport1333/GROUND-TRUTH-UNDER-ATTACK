"""Synthetic fixtures only, except explicitly enabled real NetCDF preflight."""
import csv
import gzip
import json
from datetime import datetime,timedelta,timezone
import numpy as np
import pytest
from netCDF4 import Dataset
from adapters.wegenernet import GridSource, HALF_HOURLY_VARIABLES, observation_id
from core.processing.hourly import HourlyEngine,temporal,spatial
from scripts.export_wegenernet_halfhourly import export
from scripts.validate_wegenernet_export import validate
from tests.test_hourly_integration import nc_fixture,event


def fixture(path, offsets):
    nc_fixture(path,offsets)
    with Dataset(path,'a') as f:
        f['time'].units='minutes since 2023-12-31 23:00:00';f['time_bnds'].units=f['time'].units
        f['time'][:]=np.array(list(offsets))*30
        f['time_bnds'][:]=[[30*(v-1),30*v] for v in offsets]
        for n in list(f.variables):
            if n.startswith(('T_zAvgTerr_H','RH_H','Precip_H')):
                f.renameVariable(n,n.replace('_HM','_HHM').replace('_HS','_HHS'))
    return path


@pytest.mark.parametrize('n',range(1,50))
def test_duration_and_missing(n):
    f=temporal([2.]*n,n,True,2)
    for key,required,value in [('std_3h',6,0.),('std_6h',12,0.),('mean_24h',48,2.),
        ('delta_30min',2,0.),('delta_1h',3,0.),('zero_diff_count_6h',13,12.),('zero_diff_count_24h',49,48.)]:
        assert f[key]==(value if n>=required else None)
    assert temporal([2.]*(n-1)+[None],0,True,2)['latest'] is None


def test_spike_exact_equality_and_week():
    f=temporal([1.]*12+[4.],1,False,2)
    assert f['delta_30min']==f['delta_1h']==f['max_abs_delta_6h']==3.
    assert f['zero_diff_count_6h']==11.
    assert f['std_6h']==pytest.approx(np.std([1.]*11+[4.]))
    assert temporal([1.]*12+[1.+1e-12],1,False,2)['zero_diff_count_6h']==11.
    assert temporal([1.]*335,335,True,2)['mean_168h'] is None
    assert temporal([1.]*336,336,True,2)['mean_168h']==1.
    assert temporal([1.,None,3.],1,True,2)['delta_1h'] is None


def test_year_boundary_resume_and_exports(tmp_path):
    path=fixture(tmp_path/'fixture.nc',range(1,8))
    start='2023-12-31T23:00:00Z';end='2024-01-01T02:30:00Z'
    source=GridSource([path],[(1,1)],start,end,interval_seconds=1800)
    assert source.variables==HALF_HOURLY_VARIABLES
    assert source.available_warmup==0
    out=tmp_path/'out';export([path],[(1,1)],out,start,end)
    before={p.relative_to(out):p.read_bytes() for p in out.glob('????-??/*')}
    result=export([path],[(1,1)],out,start,end,resume=True)
    assert result['rows']==7
    assert before=={p.relative_to(out):p.read_bytes() for p in out.glob('????-??/*')}
    assert validate(out,[path])['rows']==7
    # An incomplete partition is rebuilt using the same preceding history/chains.
    old_rows=gzip.decompress((out/'2024-01'/'compact.csv.gz').read_bytes())
    old_windows=gzip.decompress((out/'2024-01'/'windows.jsonl.gz').read_bytes())
    (out/'2024-01'/'SUCCESS.json').unlink()
    export([path],[(1,1)],out,start,end,resume=True)
    assert validate(out,[path])['rows']==7
    assert gzip.decompress((out/'2024-01'/'compact.csv.gz').read_bytes())==old_rows
    assert gzip.decompress((out/'2024-01'/'windows.jsonl.gz').read_bytes())==old_windows
    with gzip.open(out/'2024-01'/'compact.csv.gz','rt') as f:
        first=next(csv.DictReader(f));assert first['T_zAvgTerr_HHM__delta_1h']=='2.0'
    with pytest.raises(ValueError):GridSource([path,path],[(1,1)],start,end,interval_seconds=1800)


def test_halfhour_invalid_and_spatial():
    sid='wn_grid_y1_x1';name='T_zAvgTerr_HHM'
    engine=HourlyEngine([sid],{sid:{}},{name:('K','Flagged_T')},1800)
    start=datetime(2024,1,1,tzinfo=timezone.utc)
    for i,status in enumerate(['valid','bad_signature','valid']):
        t=start+timedelta(minutes=30*i)
        e=event(283.,time=t,name=name,status=status)
        w,row,_=engine.process(t,[e],[dict(observation_id=str(i),flag_percent=50.)])[0]
        assert w.end-w.start==timedelta(minutes=30)
        assert row[name+'__delta_1h'] is None
        if status!='valid':assert row[name+'__latest'] is None
    assert spatial(280.,dict(ym=283.,yp=280.,xm=280.,xp=280.))['minus_neighbor_median_4']==0.
    assert spatial(283.,dict(ym=280.,yp=280.,xm=280.,xp=280.))['minus_neighbor_median_4']==3.


@pytest.mark.parametrize('year,days',[(2023,28),(2024,29)])
def test_february(year,days):
    a=datetime(year,2,1,tzinfo=timezone.utc);b=datetime(year,3,1,tzinfo=timezone.utc)
    assert (b-a).total_seconds()/1800==days*48


def test_warmup_alignment_flags_and_ids(tmp_path):
    path=fixture(tmp_path/'source.nc',range(1,8))
    s=GridSource([path],[(1,1)],'2024-01-01T00:00:00Z','2024-01-01T02:30:00Z',interval_seconds=1800)
    assert s.available_warmup==2
    records=list(s.hours());t,samples,meta=records[0]
    assert len({m['observation_id'] for _,_,ms in records for m in ms})==7*5*5
    m=meta[0]
    assert m['flag_percent']==0.0625
    assert observation_id(m['file_sha256'],m['variable'],t,m['y'],m['x'])==m['observation_id']
    with Dataset(path,'a') as f:f['time_bnds'][0,0]=-30
    with pytest.raises(ValueError,match='intervals'):GridSource([path],[(1,1)],'2023-12-31T23:00:00Z','2024-01-01T02:30:00Z',interval_seconds=1800)


def test_real_halfhour_preflight():
    import os
    from adapters.wegenernet import parse_cells
    path=os.environ.get('WN_HALFHOURLY_FILE')
    if not path:pytest.skip('Set WN_HALFHOURLY_FILE for actual January 2023 data')
    s=GridSource([path],parse_cells('config/cells.txt'),'2023-01-01T00:00:00Z','2023-02-01T00:00:00Z',interval_seconds=1800)
    assert len(s.files[0][0])==1488 and len(s.cells)==30
    assert s.grid['shape']==[163,234] and len(s.input_cells)==150
    assert s.available_warmup==0
    t,samples,meta=next(s.hours())
    assert t.isoformat()=='2023-01-01T00:30:00+00:00'
    assert len(samples)==750 and all(m['raw_value'] is not None for m in meta)


@pytest.mark.parametrize('year,days',[(2023,28),(2024,29)])
def test_complete_february_source_preflight(tmp_path,year,days):
    path=fixture(tmp_path/'feb.nc',range(1,days*48+1))
    with Dataset(path,'a') as f:
        f['time'].units=f'minutes since {year}-02-01 00:00:00';f['time_bnds'].units=f['time'].units
    s=GridSource([path],[(1,1)],f'{year}-02-01T00:00:00Z',f'{year}-03-01T00:00:00Z',interval_seconds=1800)
    assert len(s.files[0][0])==days*48


def test_source_file_boundary_and_missing_month(tmp_path):
    a=fixture(tmp_path/'a.nc',range(1,4));b=fixture(tmp_path/'b.nc',range(4,8))
    source=GridSource([b,a],[(1,1)],'2023-12-31T23:00:00Z','2024-01-01T02:30:00Z',interval_seconds=1800)
    assert len(list(source.hours()))==7
    with pytest.raises(ValueError,match='Missing target'):
        GridSource([a],[(1,1)],'2023-12-31T23:00:00Z','2024-01-01T02:30:00Z',interval_seconds=1800)


def test_subsecond_labels_rejected(tmp_path):
    p=fixture(tmp_path/'fraction.nc',[1,2])
    with Dataset(p,'a') as f:
        f['time'].units='minutes since 2023-12-31 23:00:00.5';f['time_bnds'].units=f['time'].units
    with pytest.raises(ValueError,match='aligned'):
        GridSource([p],[(1,1)],'2023-12-31T23:00:00Z','2024-01-01T00:00:00Z',interval_seconds=1800)
