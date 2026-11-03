"""Integration uses actual signatures and retained verifier journal evidence."""
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_phase2_broker import broker, PASSWORD
from test_phase4_processing import START, END, values
from adapters.live_mqtt import LiveMQTT
from adapters.mqtt_transport import MQTTConnection
from core.contracts import FeatureWindow, SignedReading
from core.local_config import connection_settings
from core.pipeline import ProvenancePipeline
from core.processing.engine import ProcessingEngine
from core.processing.journal import read_verifier_journal
from core.provenance.codec import encode_reading
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import load_private_key
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier
from generate_keys import generate_sensor_bundle


def make_signer(tmp_path):
    bundle=tmp_path/'sensors'
    item=generate_sensor_bundle(bundle,['sensor_01'],PASSWORD)[0]
    registry=KeyRegistry.import_bundle(tmp_path/'receiver.sqlite3',bundle)
    key=load_private_key(bundle/item['private_key'],PASSWORD)
    state=SignerState.create(tmp_path/'signer.sqlite3',[(item['sensor_id'],item['key_id'],item['stream_id'])])
    return registry,SensorSigner(item['sensor_id'],item['stream_id'],key,state)


def test_crypto_journal_processing_preserves_rejections_and_nulls(tmp_path):
    registry,signer=make_signer(tmp_path)
    verifier=Verifier(registry,clock=lambda:END)
    l4=[]
    pipeline=ProvenancePipeline(verifier,lambda i,v:l4.append(v))
    original=signer.sign(68.0,unit='F',timestamp=START)
    pipeline.receive(encode_reading(original))
    pipeline.receive(encode_reading(original))
    tampered=SignedReading(**{**original.model_dump(),'value':999.0})
    pipeline.receive(encode_reading(tampered))
    pipeline.receive(b'invalid')
    pipeline.receive(encode_reading(signer.sign(None,timestamp=START+timedelta(seconds=1))))
    pipeline.receive(encode_reading(signer.sign(25.0,unit='unknown',timestamp=START+timedelta(seconds=2))))
    pipeline.receive(encode_reading(signer.sign(1000.0,timestamp=START+timedelta(seconds=3))))
    assert len(l4)==7 and not verifier.pending()
    # Delivered events remain evidence, including the null and invalid unit.
    events=read_verifier_journal(KeyRegistry(registry.path))
    result=ProcessingEngine().build_window(events,start=START,end=END,as_of=END)
    assert len(result.window.readings)==4 and len(result.excluded_events)==3
    assert [p.normalized_value for p in result.window.readings]==[20.0,None,None,1000.0]
    assert FeatureWindow.model_validate_json(result.window.model_dump_json())==result.window
    assert values(result.window)['usable_count']==2
    with pytest.raises(BufferError): read_verifier_journal(registry,limit=1)


def test_actual_window_cli(tmp_path):
    registry,signer=make_signer(tmp_path)
    Verifier(registry,clock=lambda:END).verify(encode_reading(signer.sign(298.15,unit='K',timestamp=START)))
    command=[sys.executable,'-m','scripts.build_feature_window','--state',str(registry.path),
             '--start',START.isoformat(),'--end',END.isoformat(),'--as-of',END.isoformat()]
    result=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    output=json.loads(result.stdout)
    window=FeatureWindow.model_validate_json(json.dumps(output['window']))
    assert window.readings[0].normalized_value==pytest.approx(25.0)
    assert output['policy']['window_seconds']==60


def test_actual_processing_demo():
    result=subprocess.run([sys.executable,'-m','scripts.demo_processing'],cwd=Path(__file__).resolve().parents[1],
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    first,serialized=result.stdout.split('\n',1)
    assert 'PHASE 4 DEMO PASS' in first
    window=FeatureWindow.model_validate_json(serialized)
    assert len(window.readings)==48
    assert window.spatial_features.availability=='available'
    assert window.reference_features.availability=='available'


@pytest.mark.broker
@pytest.mark.integration
def test_real_mtls_verifier_to_feature_window(broker,tmp_path):
    root,manifest=broker
    registry=KeyRegistry.import_bundle(tmp_path/'receiver.sqlite3',root/'sensors')
    verifier=Verifier(registry,clock=lambda:END)
    pipeline=ProvenancePipeline(verifier,lambda i,v:None)
    state=SignerState.create(tmp_path/'signer.sqlite3',[(s['sensor_id'],s['key_id'],s['stream_id']) for s in manifest['sensors']])
    with LiveMQTT(connection_settings(root,manifest,'observer'),pipeline) as receiver:
        for item,unit in zip(manifest['sensors'],('C','F')):
            key=load_private_key(root/'sensors'/item['private_key'],PASSWORD)
            signer=SensorSigner(item['sensor_id'],item['stream_id'],key,state)
            with MQTTConnection(connection_settings(root,manifest,item['sensor_id'])) as publisher:
                for i in range(6):
                    celsius=20.0+i
                    raw=signer.sign(celsius if unit=='C' else celsius*1.8+32,unit=unit,timestamp=START+timedelta(seconds=i*2))
                    publisher.publish(item['sensor_id'],encode_reading(raw))
                    assert receiver.receive(5).status=='valid'
    events=read_verifier_journal(registry)
    result=ProcessingEngine().build_window(events,start=START,end=END,as_of=END)
    assert len(result.window.readings)==12
    for sensor in ('sensor_01','sensor_02'):
        f=values(result.window,sensor=sensor)
        assert f['mean_c']==pytest.approx(22.5)
        assert f['last_rate_c_per_s']==pytest.approx(0.5)


def test_window_cli_policy_file_and_rejects_invalid_duration(tmp_path):
    from core.processing.policy import ProcessingPolicy
    registry,signer=make_signer(tmp_path)
    policy=tmp_path/'processing.json'
    policy.write_text(ProcessingPolicy(rolling_samples=3).model_dump_json())
    command=[sys.executable,'-m','scripts.build_feature_window','--state',str(registry.path),
             '--start',START.isoformat(),'--end',END.isoformat(),'--as-of',END.isoformat()]
    cwd=Path(__file__).resolve().parents[1]
    result=subprocess.run([*command,'--policy',str(policy)],cwd=cwd,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['policy']['rolling_samples']==3
    result=subprocess.run([*command,'--window-seconds','0'],cwd=cwd,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0
