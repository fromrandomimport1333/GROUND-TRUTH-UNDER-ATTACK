import json
from pathlib import Path
import subprocess
import sys
import pytest
from benchmark import run_benchmark
from eval.transport_benchmark import run_transport_benchmark
from stability import run_stability


def test_actual_offline_benchmark_and_cli(tmp_path):
    result,payload=run_benchmark(tmp_path,iterations=2,series=2,warmup=1)
    assert payload and result['source']=='synthetic'
    for stage in result['stages'].values():
        assert stage['pooled']['samples']==4 and len(stage['series'])==2
        assert stage['pooled']['max_ms']>=stage['pooled']['min_ms']>=0
    sizes=result['message_sizes']
    assert sizes['signed_cbor_bytes']>sizes['without_signature_and_prev_hash_cbor_bytes']
    output=tmp_path/'benchmark.json'
    process=subprocess.run([sys.executable,'benchmark.py','--iterations','1','--series','1','--warmup','0','--output',str(output)],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
    assert process.returncode==0,process.stderr
    assert json.loads(output.read_text())==json.loads(process.stdout)


@pytest.fixture
def executable(request):
    if not request.config.getoption('--run-broker'): pytest.skip('real broker tests require --run-broker')
    import shutil
    result=request.config.getoption('--broker-executable') or shutil.which('mosquitto')
    if not result: pytest.fail('Mosquitto required')
    return Path(result)


@pytest.mark.broker
@pytest.mark.integration
def test_actual_tls_plaintext_comparison(tmp_path,executable):
    result=run_transport_benchmark(tmp_path/'lab',executable,b'synthetic-only',iterations=2,series=1,warmup=1)
    for mode in ('mtls','plaintext_loopback_control'):
        assert result[mode]['publish_to_subscriber_callback']['pooled']['samples']==2
        assert result[mode]['publish_to_broker_puback']['pooled']['mean_ms']>=0


@pytest.mark.broker
@pytest.mark.integration
def test_actual_multisensor_faults_and_restart(tmp_path,executable):
    result=run_stability(tmp_path/'lab',executable,duration=60.0,interval=.01,max_rounds=12)
    assert result['rounds']==12 and result['issued_records']==37
    assert result['audited_status_counts']=={'valid':37,'gap':1,'replay':2}
    assert result['sender_receiver_heads_match'] and result['all_planned_faults_exercised']
    assert all(n>=2 for n in result['publisher_connection_attempts'])
    assert result['archive']['records']==37 and result['pending_sender_records']==0


@pytest.mark.broker
@pytest.mark.integration
def test_real_receiver_automatically_reconnects_after_broker_restart(tmp_path,executable):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from adapters.reconnect import ReconnectingReceiver, ReconnectingPublisher
    from core.local_config import connection_settings
    from core.pipeline import ProvenancePipeline
    from core.provenance.codec import encode_reading
    from core.provenance.keyregistry import KeyRegistry
    from core.provenance.keys import load_private_key
    from core.provenance.signer import SensorSigner
    from core.provenance.state import SignerState
    from core.provenance.verifier import Verifier
    from eval.local_broker import LocalBroker
    with LocalBroker(tmp_path/'auto-reconnect',executable) as broker:
        registry=KeyRegistry.import_bundle(broker.root/'receiver.sqlite3',broker.root/'sensors')
        audit=[]
        pipeline=ProvenancePipeline(Verifier(registry),lambda i,v:audit.append(v))
        with ReconnectingReceiver(connection_settings(broker.root,broker.manifest,'observer'),pipeline) as receiver:
            old=receiver.receiver
            broker.stop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(receiver.receive,5)
                broker.start()
                deadline=time.monotonic()+8
                while receiver.receiver is old or receiver.receiver is None or not receiver.receiver.connection.subscribed.is_set():
                    if future.done(): future.result()
                    if time.monotonic()>deadline: pytest.fail('automatic receiver reconnect timed out')
                    time.sleep(.02)
                item=broker.manifest['sensors'][0]
                key=load_private_key(broker.root/'sensors'/item['private_key'],broker.password)
                signer=SensorSigner(item['sensor_id'],item['stream_id'],key,SignerState(broker.root/'signer.sqlite3'))
                reading=signer.sign(25.0)
                sender=ReconnectingPublisher(connection_settings(broker.root,broker.manifest,item['sensor_id']))
                try: sender.publish(item['sensor_id'],encode_reading(reading))
                finally: sender.close()
                assert future.result(timeout=8).status=='valid'
            assert receiver.connection_attempts>=2 and len(audit)==1
