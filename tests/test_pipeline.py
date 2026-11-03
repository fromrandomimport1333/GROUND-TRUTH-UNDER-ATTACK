"""Phase 5 integration: real signatures, production verifier/processing, stub L3."""
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
from typing import get_args

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adapters.replay import ReplayAdapter, ReplayRecord
from adapters.dataset import Measurement
from attacks.false_data import AttackConfig, Family, inject
from attacks.scenario import sign_experiment
from attacks.wire import break_chain, tamper_temperature
from core.contracts import FeatureWindow
from core.pipeline import WindowPipeline
from core.processing.engine import ProcessingEngine
from core.provenance.codec import decode_reading, encode_reading
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import key_id
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier
from scripts.demo_attacks import synthetic_measurements

START = synthetic_measurements()[0].observed_at
END = START+timedelta(seconds=60)


def crypto(tmp_path):
    key = Ed25519PrivateKey.generate()
    registry = KeyRegistry.create(tmp_path/'receiver.sqlite3')
    registry.enroll('sensor_01',key.public_key(),'stream_01')
    state = SignerState.create(tmp_path/'signer.sqlite3',[('sensor_01',key_id(key),'stream_01')])
    signer = SensorSigner('sensor_01','stream_01',key,state)
    payloads = tuple(encode_reading(signer.sign(20.0+i,timestamp=START+timedelta(seconds=i))) for i in range(3))
    return key,registry,payloads


def run(registry,payloads,**kwargs):
    audit,received=[],[]
    adapter = ReplayAdapter(registry,layer4=lambda i,v:audit.append((i,v)),layer3=received.append,**kwargs)
    result = adapter.run(tuple(ReplayRecord(payload=p,received_at=END) for p in payloads),as_of=END)
    return result,audit,received,adapter


@pytest.mark.parametrize('case,expected',[
    ('tamper',['bad_signature','valid','valid','valid']),
    ('unknown',['unknown_sensor']),
    ('duplicate',['valid','replay','valid','valid']),
    ('delete',['valid','gap']),
    ('reorder',['gap','valid','valid','valid']),
    ('chain',['valid','chain_anomaly','valid','valid']),
    ('old',['out_of_window']),
    ('malformed',['malformed']),
])
def test_wire_security_and_recovery(tmp_path,case,expected):
    key,registry,p = crypto(tmp_path)
    if case=='tamper': records=(tamper_temperature(p[0],999.0),*p)
    elif case=='unknown':
        registry=KeyRegistry.create(tmp_path/'unknown.sqlite3')
        records=p[:1]
    elif case=='duplicate': records=(p[0],*p)
    elif case=='delete': records=(p[0],p[2])
    elif case=='reorder': records=(p[1],*p)
    elif case=='chain': records=(p[0],break_chain(p[1],key),p[1],p[2])
    elif case=='old':
        from core.provenance.replay_protection import ReplayPolicy
        result,audit,received,_=run(registry,p[:1],replay_policy=ReplayPolicy(max_age_seconds=1))
    else: records=(b'bad',)
    if case!='old': result,audit,received,_=run(registry,records)
    assert [v.status for v in result.events]==expected
    assert len(audit)==len(expected)
    assert all(not item.ready_for_analysis for w in received for item in w.readings if item.provenance.status!='valid')
    if case in ('tamper','duplicate','reorder','chain'):
        assert [r.normalized_value for r in received[0].readings]==[20.0,21.0,22.0]
    if case=='malformed':
        assert not received and len(result.unassigned)==1


@pytest.mark.parametrize('family', get_args(Family))
def test_all_signed_attacks_reach_features_without_labels(tmp_path,family):
    cfg=AttackConfig(family=family,sensors=('sensor_01','sensor_02'),start_index=3,template_c=(19.0,20.0))
    batch=inject(synthetic_measurements(),cfg)
    recording=sign_experiment(batch.measurements,tmp_path/'signers')
    registry=KeyRegistry.create(tmp_path/'receiver.sqlite3')
    recording.enroll(registry)
    result,audit,received,_=run(registry,[r.payload for r in recording.records])
    assert len(audit)==48 and all(v.status=='valid' for v in result.events)
    assert len(received)==1 and isinstance(received[0],FeatureWindow)
    assert all(p.ready_for_analysis for p in received[0].readings)
    assert received[0].temporal_features.availability=='available'
    encoded=received[0].model_dump_json()
    assert 'original_value' not in encoded and 'family' not in encoded and 'source_record_id' not in encoded
    for raw,measurement in zip(result.events,batch.measurements):
        assert raw.reading.value==measurement.value
    assert recording.source_records==tuple((m.source_id,m.source_record_id) for m in batch.measurements)


def test_recording_replay_exact_reproducibility(tmp_path):
    recording=sign_experiment(synthetic_measurements(),tmp_path/'signers')
    results=[]
    for index in range(2):
        registry=KeyRegistry.create(tmp_path/f'receiver{index}.sqlite3')
        recording.enroll(registry)
        received=[]
        result=ReplayAdapter(registry,layer4=lambda i,v:None,layer3=received.append).run(recording.records,as_of=END)
        results.append(result)
    assert results[0]==results[1]
    assert results[0].windows[0].window.window_id==results[1].windows[0].window.window_id


def test_replay_validation_and_no_live_registry_reset(tmp_path):
    _,registry,p=crypto(tmp_path)
    adapter=ReplayAdapter(registry,layer4=lambda i,v:None,layer3=lambda w:None)
    with pytest.raises(ValueError): adapter.run((ReplayRecord(payload=p[0],received_at=END),),as_of=START)
    with pytest.raises(ValueError): adapter.run((ReplayRecord(payload=p[0],received_at=END),ReplayRecord(payload=p[1],received_at=START)),as_of=END)
    with pytest.raises(BufferError): adapter.run((ReplayRecord(payload=p[0],received_at=START) for _ in range(10001)),as_of=END)
    assert not adapter.used
    Verifier(registry,clock=lambda:END).verify(p[0])
    with pytest.raises(ValueError,match='fresh'): adapter.run((),as_of=END)


def test_callback_failure_retains_l4_evidence(tmp_path):
    _,registry,p=crypto(tmp_path)
    def fail(i,v): raise RuntimeError('sink down')
    adapter=ReplayAdapter(registry,layer4=fail,layer3=lambda w:None)
    with pytest.raises(RuntimeError,match='sink down'):
        adapter.run((ReplayRecord(payload=p[0],received_at=END),),as_of=END)
    pending=Verifier(registry).pending()
    assert len(pending)==1 and pending[0][1].status=='valid'
    with pytest.raises(RuntimeError,match='single-use'): adapter.run((),as_of=END)


def test_layer3_failure_propagates_and_explicit_redelivery(tmp_path):
    _,registry,p=crypto(tmp_path)
    result,_,_,adapter=run(registry,p)
    with pytest.raises(RuntimeError,match='single-use'): adapter.run((),as_of=END)
    def fail(w): raise RuntimeError('detector down')
    pipe=WindowPipeline(ProcessingEngine(),fail)
    with pytest.raises(RuntimeError,match='detector down'): pipe.deliver(result.windows[0].window)
    received=[]
    WindowPipeline(ProcessingEngine(),received.append).deliver(result.windows[0].window)
    assert received[0]==result.windows[0].window
    with pytest.raises(TypeError): pipe.deliver({})


def test_demo_subprocess():
    process=subprocess.run([sys.executable,'-m','scripts.demo_attacks'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
    assert process.returncode==0,process.stderr
    report=json.loads(process.stdout)
    assert len(report['experiments'])==8
    assert len(report['security_scenarios'])==6
    assert report['security_scenarios'][0]['statuses'][0]=='bad_signature'
    assert all(e['statuses']=={'valid':48} and not e['detection_evaluated'] for e in report['experiments'])


from test_phase2_broker import broker, PASSWORD


@pytest.mark.broker
@pytest.mark.integration
def test_real_mtls_signed_false_data_and_tampering_to_layer3(broker,tmp_path):
    from adapters.live_mqtt import LiveMQTT
    from adapters.mqtt_transport import MQTTConnection
    from core.local_config import connection_settings
    from core.pipeline import ProvenancePipeline
    from core.provenance.keys import load_private_key
    from core.processing.journal import read_verifier_journal
    root,manifest=broker
    item=manifest['sensors'][0]
    registry=KeyRegistry.import_bundle(tmp_path/'live.sqlite3',root/'sensors')
    key=load_private_key(root/'sensors'/item['private_key'],PASSWORD)
    state=SignerState.create(tmp_path/'live_signer.sqlite3',[(item['sensor_id'],item['key_id'],item['stream_id'])])
    signer=SensorSigner(item['sensor_id'],item['stream_id'],key,state)
    baseline=Measurement(sensor_id=item['sensor_id'],observed_at=START,value=20.0,
                         source_id='synthetic_test',source_record_id='0')
    injected=inject((baseline,),AttackConfig(family='bias',sensors=(item['sensor_id'],),offset_c=10.0)).measurements[0]
    payload=encode_reading(signer.sign(injected.value,timestamp=START))
    audit=[]
    pipeline=ProvenancePipeline(Verifier(registry,clock=lambda:END),lambda i,v:audit.append(v))
    with LiveMQTT(connection_settings(root,manifest,'observer'),pipeline) as receiver:
        with MQTTConnection(connection_settings(root,manifest,item['sensor_id'])) as publisher:
            for wire,status in ((tamper_temperature(payload,99.0),'bad_signature'),(payload,'valid'),(payload,'replay')):
                publisher.publish(item['sensor_id'],wire)
                assert receiver.receive(5).status==status
    received=[]
    results,_=WindowPipeline(ProcessingEngine(),received.append).process(read_verifier_journal(registry),as_of=END)
    assert len(audit)==3 and len(received)==1
    assert received[0].readings[0].normalized_value==30.0
    assert len(results[0].excluded_events)==2
