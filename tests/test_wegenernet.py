import json
import numpy as np
import pytest
from netCDF4 import Dataset
from adapters.wegenernet import hours,number
from scripts.export_wegenernet import export
from core.contracts import FeatureWindow

def fixture_file(path):
    with Dataset(path,'w') as f:
        for n,s in [('time',3),('Y',1),('X',2),('nv',2)]:f.createDimension(n,s)
        f.createVariable('UTM33N','i4').epsg_code='EPSG:32633'
        f.createVariable('X','f4',('X',))[:]=[556650,556750]
        f.createVariable('Y','f4',('Y',))[:]=[5189850]
        t=f.createVariable('time','i4',('time',));t.units='hours since 2024-01-01 00:00:00';t.calendar='gregorian';t[:]=[1,2,3]
        b=f.createVariable('time_bnds','i4',('time','nv'));b.units=t.units;b[:]=[[0,1],[1,2],[2,3]]
        v=f.createVariable('T_zAvgTerr_HM','f4',('time','Y','X'),fill_value=-999.);v.units='K';v[:]=[[[273.15,274.15]],[[275.15,-999]],[[276.15,277.15]]]
        f.createVariable('Flagged_T','f4',('time','Y','X'))[:]=0
    return path

def test_read(tmp_path):
    p=fixture_file(tmp_path/'a.nc'); rows=list(hours([p],[(0,0),(0,1)]))
    assert rows[0][0].hour==1
    assert rows[1][1][1].value is None
    assert rows[0][1][0].location.latitude>40
    assert number(np.nan) is None

def test_bounds_and_duplicates(tmp_path):
    p=fixture_file(tmp_path/'a.nc')
    with pytest.raises(ValueError):list(hours([p,p],[(0,0)]))
    with pytest.raises(ValueError):list(hours([p],[(-1,0)]))
    with Dataset(p,'a') as f:f['time_bnds'][0]=[0,2]
    with pytest.raises(ValueError):list(hours([p],[(0,0)]))

def test_integration(tmp_path):
    p=fixture_file(tmp_path/'a.nc');out=tmp_path/'out'
    assert export([p],[(0,0),(0,1)],out)['readings']==6
    w=FeatureWindow.model_validate_json((out/'windows.jsonl').read_text().strip())
    assert len(w.readings)==6
    assert sum(r.ready_for_analysis for r in w.readings)==5
    assert abs(w.readings[0].normalized_value)<0.001
    assert all(r.provenance.signature_verified for r in w.readings)
    assert not json.loads((out/'evidence.jsonl').read_text())['complete_24_hours']
