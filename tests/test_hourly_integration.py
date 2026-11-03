"""All generated NetCDFs and signed envelopes here are synthetic TEST FIXTURES."""
import csv
from datetime import datetime,timedelta,timezone
import gzip
import json
import os
from pathlib import Path

import numpy as np
from netCDF4 import Dataset
import pytest

from adapters.wegenernet import (GridSource, VARIABLES, parse_cells, observation_id, cell_id)
from core.contracts import FeatureWindow,SignedReading,VerifiedReading
from core.processing.hourly import HourlyEngine,temporal,spatial
from scripts.export_wegenernet_hourly import export_hourly

START = datetime(2024,1,1,tzinfo=timezone.utc)


def nc_fixture(path, offsets=range(1,28), shape=(3,3)):
    offsets=list(offsets)
    with Dataset(path,'w') as f:
        for name,size in [('time',len(offsets)),('Y',shape[0]),('X',shape[1]),('nv',2)]:
            f.createDimension(name,size)
        f.createVariable('UTM33N','i4').epsg_code='EPSG:32633'
        for name,n in zip(('Y','X'),shape):
            f.createVariable(name,'f4',(name,))[:] = np.arange(n)*100+500000
        t=f.createVariable('time','i4',('time',));t.units='hours since 2024-01-01';t.calendar='gregorian';t[:]=offsets
        b=f.createVariable('time_bnds','i4',('time','nv'));b.units=t.units;b[:]=[[v-1,v] for v in offsets]
        for name,(unit,flag) in VARIABLES.items():
            v=f.createVariable(name,'f4',('time','Y','X'),fill_value=np.nan);v.units=unit
            v[:]=np.asarray(offsets)[:,None,None]+(270 if unit=='K' else 0)
        for name in ('Flagged_T','Flagged_RH','Flagged_Precip'):
            v=f.createVariable(name,'f4',('time','Y','X'),fill_value=101.);v.units='%';v[:]=0.0625
    return path


def event(value,time=START,sid='wn_grid_y1_x1',name='T_zAvgTerr_HM',status='valid'):
    raw=SignedReading(schema_version='1.0',sensor_id=sid,key_id='test',stream_id='test',
        sequence=int((time-START).total_seconds()),quantity=name,value=value,unit='K',
        timestamp=time.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),prev_hash=bytes(32),signature=bytes(64))
    return VerifiedReading(schema_version='1.0',reading=raw,status=status,
        signature_verified=status!='bad_signature',checked_at=time)


def test_cells_encodings_duplicates_and_format(tmp_path):
    p=tmp_path/'cells.txt'
    for encoding in ('utf-16','utf-8-sig','utf-8'):
        p.write_text('wn_grid_y1_x2\nwn_grid_y0_x0\n',encoding=encoding)
        assert parse_cells(p)==[(1,2),(0,0)]
    for text in ('wn_grid_y1_x2\nwn_grid_y1_x2','wn_grid_y-1_x2','wn_grid_y01_x2','anything',''):
        p.write_text(text)
        with pytest.raises(ValueError):parse_cells(p)


def test_grid_and_month_boundary(tmp_path):
    a=nc_fixture(tmp_path/'z.nc',range(1,4));b=nc_fixture(tmp_path/'a.nc',range(4,7))
    src=GridSource([b,a],[(1,1)],START,START+timedelta(hours=6))
    assert src.available_warmup==0
    assert len(src.neighbors['wn_grid_y1_x1'])==4
    assert len(src.input_cells)==5
    assert len(list(src.hours()))==6
    edge=GridSource([a],[(0,0)],START,START+timedelta(hours=3))
    assert len(edge.neighbors['wn_grid_y0_x0'])==2
    for files,cells,end in [([a],[(3,1)],3),([a,a],[(1,1)],3),([a],[(1,1)],6)]:
        with pytest.raises(ValueError):GridSource(files,cells,START,START+timedelta(hours=end))
    with pytest.raises(FileNotFoundError):GridSource([tmp_path/'missing.nc'],[(1,1)],START,START+timedelta(hours=3))
    warm=GridSource([a,b],[(1,1)],START+timedelta(hours=3),START+timedelta(hours=6))
    assert warm.available_warmup==3


def test_leap_year_and_source_gap(tmp_path):
    feb=datetime(2024,2,1,tzinfo=timezone.utc)
    march=datetime(2024,3,1,tzinfo=timezone.utc)
    assert (march-feb).total_seconds()/3600==696
    assert (datetime(2025,1,1,tzinfo=timezone.utc)-START).total_seconds()/3600==8784
    a=nc_fixture(tmp_path/'a.nc',[1,3])
    with pytest.raises(ValueError,match='contiguous'):GridSource([a],[(1,1)],START,START+timedelta(hours=3))


def test_temporal_exact_definitions():
    for n in range(1,26):
        result=temporal([2.0]*n,n,True)
        assert result['std_3h']==(0. if n>=3 else None)
        assert result['std_6h']==(0. if n>=6 else None)
        assert result['zero_diff_count_6h']==(6. if n>=7 else None)
        assert result['zero_diff_count_24h']==(24. if n>=25 else None)
        assert result['mean_24h']==(2. if n>=24 else None)
    spike=temporal([1.]*6+[4.],1,False)
    assert spike['max_abs_delta_6h']==3
    assert spike['zero_diff_count_6h']==5
    assert spike['std_3h']==pytest.approx(np.std([1.,1.,4.]))
    assert temporal([1.]*6+[None],0,True)['std_3h'] is None
    assert temporal([1.]*6+[1.+1e-12],1,False)['zero_diff_count_6h']==5


def test_spatial_three_kelvin_attack_and_boundaries():
    clean=dict(ym=280.,yp=280.,xm=280.,xp=280.)
    assert spatial(283.,clean)['minus_neighbor_median_4']==3.
    contaminated=dict(clean,ym=283.)
    assert spatial(280.,contaminated)['minus_neighbor_mean']==-.75
    assert spatial(280.,contaminated)['minus_neighbor_median_4']==0.
    assert spatial(280.,{'ym':283.,'yp':280.})['minus_neighbor_mean']==-1.5
    assert spatial(280.,{'ym':280.,'yp':280.})['minus_neighbor_median_4'] is None
    assert spatial(None,clean)['minus_neighbor_mean'] is None
    assert spatial(280.,dict(clean,ym=None))['minus_neighbor_median_4'] is None


def test_ids_are_stable_and_disambiguated():
    a=observation_id('digest','T',START,1,2)
    assert a==observation_id('digest','T',START,1,2)
    assert len({a,observation_id('different','T',START,1,2),observation_id('digest','RH',START,1,2),
                observation_id('digest','T',START,2,2),observation_id('digest','T',START+timedelta(hours=1),1,2)})==5


@pytest.mark.parametrize('status',['bad_signature','replay','gap','chain_anomaly','out_of_window'])
def test_rejected_samples_never_enter_history(status):
    sid='wn_grid_y1_x1';name='T_zAvgTerr_HM'
    e=HourlyEngine([sid],{sid:{}},{name:('K','Flagged_T')})
    for n,(value,verdict) in enumerate([(280.,'valid'),(999.,status),(280.,'valid')]):
        t=START+timedelta(hours=n)
        w,row,lineage=e.process(t,[event(value,t,status=verdict)],[{'observation_id':str(n)}])[0]
        FeatureWindow.model_validate_json(w.model_dump_json())
        if n==1:
            assert row[name+'__latest'] is None
            assert not row[name+'__eligible']
            assert w.readings[0].provenance.reading.value==999.
        if n==2:
            assert row[name+'__std_3h'] is None
            assert row[name+'__delta_1h'] is None
            assert row[name+'__run_length_identical']==1.


def test_absent_hours_nulls_and_reference_unavailable():
    sid='wn_grid_y1_x1';name='T_zAvgTerr_HM'
    engine=HourlyEngine([sid],{sid:{}},{name:('K','Flagged_T')})
    engine.process(START,[event(280.)],[{}])
    w,row,_=engine.process(START+timedelta(hours=2),[],[])[0]
    assert row[name+'__status']=='missing'
    assert row[name+'__latest'] is None
    assert row[name+'__history_count']==1
    assert w.reference_features.availability=='unavailable' and not w.reference_data


def test_complete_export_and_lineage(tmp_path):
    source=nc_fixture(tmp_path/'data.nc',range(1,8))
    out=tmp_path/'out'
    summary=export_hourly([source],[(1,1)],out,START,START+timedelta(hours=7))
    assert summary['cell_hours']==7
    assert summary['statuses']=={'valid':175}
    with gzip.open(out/'compact.csv.gz','rt') as f:rows=list(csv.DictReader(f))
    with gzip.open(out/'windows.jsonl.gz','rt') as f:windows=[FeatureWindow.model_validate_json(line) for line in f]
    with gzip.open(out/'observations.jsonl.gz','rt') as f:audit=[json.loads(line) for line in f]
    ids={v['source']['observation_id'] for v in audit}
    assert len(ids)==175
    with gzip.open(out/'lineage.jsonl.gz','rt') as f:
        for line in f:
            for history in json.loads(line)['history'].values():
                assert history['first_id'] in ids and history['last_id'] in ids
    for row,w in zip(rows,windows):
        for group in (w.temporal_features,w.spatial_features):
            for value in group.values:
                assert (float(row[value.name]) if row[value.name] else None)==value.value
        assert row['T_zAvgTerr_HM__raw_flag_percent']=='0.0625'
        assert row['T_zAvgTerr_HM__eligible']=='True'
        assert row['T_zAvgTerr_HM__flagged_observation_count']=='1.0'
    assert float(rows[-1]['T_zAvgTerr_HM__max_abs_delta_6h'])==1.
    assert all(r.provenance.signature_verified for w in windows for r in w.readings)
    from scripts.validate_wegenernet_export import validate
    assert validate(out,[source])['rows']==7


def test_flag_fill_not_quarantine(tmp_path):
    p=nc_fixture(tmp_path/'data.nc',[1])
    with Dataset(p,'a') as f:f['Flagged_T'][0,1,1]=101.
    s=GridSource([p],[(1,1)],START,START+timedelta(hours=1))
    _,samples,meta=next(s.hours())
    m=next(m for m in meta if m['sensor_id']=='wn_grid_y1_x1' and m['variable']=='T_zAvgTerr_HM')
    assert m['flag_percent'] is None and m['raw_value'] is not None


def test_actual_january_preflight():
    path=os.environ.get('WN_JANUARY_FILE')
    if not path:pytest.skip('Set WN_JANUARY_FILE for actual NetCDF integration')
    cells=parse_cells(Path(__file__).parents[1]/'config/cells.txt')
    source=GridSource([path],cells,START,datetime(2024,2,1,tzinfo=timezone.utc))
    assert len(cells)==30 and source.available_warmup==0
    assert sum(len(times) for times,_ in source.files)==744
    assert all(len(n)==4 for n in source.neighbors.values())


def test_full_leap_year_preflight_synthetic_only(tmp_path):
    # Pure fixture; not a replacement for missing 2024 source observations.
    p=nc_fixture(tmp_path/'synthetic-year.nc',range(1,8785))
    source=GridSource([p],[(1,1)],START,datetime(2025,1,1,tzinfo=timezone.utc))
    assert len(source.files[0][0])==8784
    assert source.files[0][0][-1]==datetime(2025,1,1,tzinfo=timezone.utc)


def test_multi_file_features_equal_single_file_and_ids_rename(tmp_path):
    a=nc_fixture(tmp_path/'first.nc',range(1,4));b=nc_fixture(tmp_path/'second.nc',range(4,8))
    whole=nc_fixture(tmp_path/'whole.nc',range(1,8))
    for paths,name in [([b,a],'split'),([whole],'whole')]:
        export_hourly(paths,[(1,1)],tmp_path/name,START,START+timedelta(hours=7))
    def load(name):
        with gzip.open(tmp_path/name/'compact.csv.gz','rt') as f:return list(csv.DictReader(f))
    split,whole_rows=load('split'),load('whole')
    for x,y in zip(split,whole_rows):
        for field in x:
            if field!='window_id' and not field.endswith('__observation_id'):assert x[field]==y[field]
    before=next(GridSource([a],[(1,1)],START,START+timedelta(hours=3)).hours())[2]
    a.rename(tmp_path/'renamed.nc')
    after=next(GridSource([tmp_path/'renamed.nc'],[(1,1)],START,START+timedelta(hours=3)).hours())[2]
    assert before==after


def test_real_signing_attacked_values_and_replay(tmp_path):
    from attacks.scenario import DatasetExperiment
    from adapters.dataset import Measurement
    from core.processing.validator import validate_reading
    sid='wn_grid_y1_x1';name='T_zAvgTerr_HM'
    session=DatasetExperiment(tmp_path,[sid],'synthetic-test')
    engine=HourlyEngine([sid],{sid:{}},{name:('K','Flagged_T')})
    for i,value in enumerate((280.,283.)):
        t=START+timedelta(hours=i)
        sample=Measurement(sensor_id=sid,observed_at=t,quantity=name,value=value,unit='K',source_id='synthetic',source_record_id=str(i))
        eid,verdict,wire=session.verify(sample)
        session.verifier.acknowledge(eid)
        w,row,_=engine.process(t,[verdict],[{}])[0]
    assert row[name+'__delta_1h']==3.
    _,replayed=session.verifier.verify(wire,topic=f'sensors/raw/{sid}')
    assert replayed.status=='replay' and not validate_reading(replayed).ready_for_analysis
    with session.registry.transaction() as db:
        plan=db.execute('EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events WHERE delivered=0').fetchall()
        assert any('events_pending' in str(r) for r in plan)


def test_opt_in_all_verdict_routing_preserves_default(verified):
    from core.pipeline import ProvenancePipeline
    rejected=VerifiedReading(**{**verified.model_dump(),'status':'replay','signature_verified':True})
    class FakeVerifier:
        def pending(self):return [(1,verified),(2,rejected)]
        def acknowledge(self,eid):pass
    for opt,expected in [(False,1),(True,2)]:
        l2=[];l4=[]
        ProvenancePipeline(FakeVerifier(),lambda i,v:l4.append(v),lambda i,v:l2.append(v),include_ineligible=opt).drain()
        assert len(l4)==2 and len(l2)==expected


def test_window_identity_changes_with_verdict():
    sid='wn_grid_y1_x1';name='T_zAvgTerr_HM';ids=[]
    for status in ('gap','replay'):
        engine=HourlyEngine([sid],{sid:{}},{name:('K','Flagged_T')})
        ids.append(engine.process(START,[event(280.,status=status)],[{}])[0][0].window_id)
    assert len(set(ids))==2


def test_deterministic_export(tmp_path):
    source=nc_fixture(tmp_path/'source.nc',[1,2])
    for name in ('a','b'):
        export_hourly([source],[(1,1)],tmp_path/name,START,START+timedelta(hours=2))
    for name in ('compact.csv.gz','windows.jsonl.gz','lineage.jsonl.gz','observations.jsonl.gz'):
        with gzip.open(tmp_path/'a'/name,'rb') as a,gzip.open(tmp_path/'b'/name,'rb') as b:
            assert a.read()==b.read()


def test_hour_alignment_and_grid_changes_fail(tmp_path):
    a=nc_fixture(tmp_path/'a.nc',[1,2]);b=nc_fixture(tmp_path/'b.nc',[3,4])
    with Dataset(b,'a') as f:f['X'][1]+=5
    with pytest.raises(ValueError,match='Grid changed'):
        GridSource([a,b],[(1,1)],START,START+timedelta(hours=4))
    with Dataset(a,'a') as f:
        f['time'].units='hours since 2024-01-01 00:30:00'
        f['time_bnds'].units=f['time'].units
    with pytest.raises(ValueError,match='hour-aligned'):
        GridSource([a],[(1,1)],START,START+timedelta(hours=2))


def test_multiple_cells_lexical_output_order(tmp_path):
    from scripts.validate_wegenernet_export import validate
    source=nc_fixture(tmp_path/'source.nc',[1],shape=(12,3))
    out=tmp_path/'out'
    export_hourly([source],[(1,1),(10,1)],out,START,START+timedelta(hours=1))
    with gzip.open(out/'compact.csv.gz','rt') as f:rows=list(csv.DictReader(f))
    assert [r['cell_id'] for r in rows]==['wn_grid_y10_x1','wn_grid_y1_x1']
    assert validate(out,[source])['rows']==2
