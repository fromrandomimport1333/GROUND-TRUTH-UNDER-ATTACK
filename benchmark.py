"""Phase 6 measured offline costs; optional isolated MQTT/TLS comparison."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import platform
import tempfile
import time

import cbor2
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from core.contracts import SignedReading
from core.provenance.codec import encode_reading, decode_reading, signing_bytes, record_hash
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import key_id
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier
from core.processing.engine import ProcessingEngine
from core.processing.validator import validate_reading
from eval.performance import measure, resources
from scripts.demo_attacks import synthetic_measurements


def run_benchmark(root, *, iterations=50, series=3, warmup=5):
    if any(type(x) is not int for x in (iterations,series,warmup)) or not 1<=iterations<=1000 or not 1<=series<=10 or not 0<=warmup<=100 or iterations*series+warmup>10000:
        raise ValueError('benchmark bounds exceeded')
    start=synthetic_measurements()[0].observed_at;end=start+timedelta(seconds=60)
    key=Ed25519PrivateKey.generate();public=key.public_key()
    identity=('sensor_01',key_id(key),'benchmark_01')
    state=SignerState.create(root/'signer.sqlite3',[identity]);signer=SensorSigner(identity[0],identity[2],key,state)
    registry=KeyRegistry.create(root/'receiver.sqlite3');registry.enroll(identity[0],public,identity[2])
    verifier=Verifier(registry,clock=lambda:end)
    events=[]
    for i in range(16):
        reading=signer.sign(20.0+i*.25,timestamp=start+timedelta(seconds=2*i))
        event_id,event=verifier.verify(encode_reading(reading));events.append(event)
        verifier.acknowledge(event_id);state.acknowledge(reading)
    raw=encode_reading(reading);message=signing_bytes(reading)
    unsigned=reading.model_dump(exclude={'signature','prev_hash'})
    full=reading.model_dump();json_data=reading.model_dump(mode='json')
    engine=ProcessingEngine()
    operations={
        'ed25519_sign_primitive':lambda:key.sign(message),
        'ed25519_verify_primitive':lambda:public.verify(reading.signature,message),
        'canonical_cbor_signed_encode':lambda:encode_reading(reading),
        'canonical_cbor_signed_encode_only':lambda:cbor2.dumps(full,canonical=True),
        'canonical_cbor_unsigned_encode':lambda:cbor2.dumps(unsigned,canonical=True),
        'bounded_cbor_decode_and_contract':lambda:decode_reading(raw),
        'json_signed_encode_hex_binary':lambda:json.dumps(json_data,separators=(',',':'),allow_nan=False).encode(),
        'chain_record_hash':lambda:record_hash(reading),
        'structural_contract_only':lambda:SignedReading.model_validate(reading),
        'normalize_only_16_readings':lambda:tuple(validate_reading(event) for event in events),
        'layer2_16_reading_window':lambda:engine.build_window(events,start=start,end=end,as_of=end),
    }
    stages={name:measure(operation,iterations=iterations,series=series,warmup=warmup) for name,operation in operations.items()}
    # Persistent stages require fresh signed sequences. ACK bookkeeping is outside each timer.
    sign_groups=[];verify_groups=[]
    from eval.performance import summarize_ns
    sign_all=[];verify_all=[]
    for group in range(series+1):
        signs=[];verifies=[]
        count=warmup if group==0 else iterations
        for _ in range(count):
            t=time.perf_counter_ns();item=signer.sign(25.0,timestamp=end-timedelta(seconds=1));sign_ns=time.perf_counter_ns()-t
            payload=encode_reading(item)
            t=time.perf_counter_ns();event_id,verdict=verifier.verify(payload);verify_ns=time.perf_counter_ns()-t
            if verdict.status!='valid': raise RuntimeError('benchmark provenance failed')
            verifier.acknowledge(event_id);state.acknowledge(item)
            signs.append(sign_ns);verifies.append(verify_ns)
        if group:
            sign_groups.append(summarize_ns(signs));verify_groups.append(summarize_ns(verifies))
            sign_all.extend(signs);verify_all.extend(verifies)
    stages['sign_with_chain_and_durable_outbox']={'series':sign_groups,'pooled':summarize_ns(sign_all)}
    stages['verify_with_decode_policy_and_durable_journal']={'series':verify_groups,'pooled':summarize_ns(verify_all)}
    signed_size=len(raw);unsigned_size=len(cbor2.dumps(unsigned,canonical=True))
    def workload():
        public.verify(reading.signature,signing_bytes(reading));decode_reading(raw)
        engine.build_window(events,start=start,end=end,as_of=end)
    comparisons={}
    for name,enabled,control in (
        ('signed_fields_encoding','canonical_cbor_signed_encode_only','canonical_cbor_unsigned_encode'),
        ('cbor_vs_json_encoding','canonical_cbor_signed_encode_only','json_signed_encode_hex_binary'),
        ('features_alignment_window_vs_normalize_only','layer2_16_reading_window','normalize_only_16_readings')):
        a=stages[enabled]['pooled']['mean_ms'];b=stages[control]['pooled']['mean_ms']
        comparisons[name]={'enabled_stage':enabled,'control_stage':control,'mean_difference_ms':a-b,'mean_ratio':a/b if b else None}
    return {'schema_version':'1.0','kind':'phase6_benchmark','source':'synthetic',
        'environment':{'python':platform.python_version(),'system':platform.system(),'machine':platform.machine()},
        'configuration':{'iterations_per_series':iterations,'series':series,'warmup':warmup},
        'stages':stages,'controlled_comparisons':comparisons,'message_sizes':{'signed_cbor_bytes':signed_size,'without_signature_and_prev_hash_cbor_bytes':unsigned_size,
            'signature_and_chain_fields_added_bytes':signed_size-unsigned_size,
            'signed_json_hex_binary_bytes':len(json.dumps(json_data,separators=(',',':')).encode())},
        'resources':resources(workload,iterations=iterations),
        'limitations':['Synthetic single-process workload; not deployment throughput.',
            'Durable stages include SQLite filesystem costs; primitive stages exclude them.',
            'Unsigned CBOR and structural-only controls do not provide equivalent security.',
            'Per-stage samples are descriptive; percentiles from small samples are coarse.',
            'No detector evaluation or performance threshold is asserted.']},raw


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations',type=int,default=50);parser.add_argument('--series',type=int,default=3)
    parser.add_argument('--warmup',type=int,default=5)
    parser.add_argument('--broker-executable',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='cis-benchmark-') as directory:
        root=Path(directory)
        report,payload=run_benchmark(root,iterations=args.iterations,series=args.series,warmup=args.warmup)
        if args.broker_executable:
            from eval.transport_benchmark import run_transport_benchmark
            report['transport']=run_transport_benchmark(root/'transport',args.broker_executable,payload,
                iterations=args.iterations,series=args.series,warmup=args.warmup)
    text=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:
        with args.output.open('x',encoding='utf-8') as output: output.write(text)
    print(text,end='')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
