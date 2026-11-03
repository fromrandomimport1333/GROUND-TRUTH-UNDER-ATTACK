"""Cryptographic rejection scenarios with independent ephemeral receiver state."""
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from adapters.replay import ReplayAdapter, ReplayRecord
from attacks.wire import break_chain, tamper_temperature
from core.provenance.codec import encode_reading
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import key_id
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState


def run_security_demo(root):
    start = datetime(2026,1,1,tzinfo=timezone.utc)
    end = start+timedelta(seconds=60)
    key = Ed25519PrivateKey.generate()
    state = SignerState.create(root/'wire_signer.sqlite3',[('sensor_01',key_id(key),'wire_01')])
    signer = SensorSigner('sensor_01','wire_01',key,state)
    p = tuple(encode_reading(signer.sign(20.0+i,timestamp=start+timedelta(seconds=i))) for i in range(3))
    cases = {
        'post_sign_tamper': ((tamper_temperature(p[0],99.0),*p), ('bad_signature','valid','valid','valid')),
        'unknown_sensor': ((p[0],), ('unknown_sensor',)),
        'replay': ((p[0],*p), ('valid','replay','valid','valid')),
        'deletion': ((p[0],p[2]), ('valid','gap')),
        'reordering': ((p[1],*p), ('gap','valid','valid','valid')),
        'signed_chain_break': ((p[0],break_chain(p[1],key),p[1],p[2]), ('valid','chain_anomaly','valid','valid')),
    }
    summaries=[]
    for name,(payloads,expected) in cases.items():
        registry=KeyRegistry.create(root/f'wire_{name}.sqlite3')
        if name!='unknown_sensor':
            registry.enroll('sensor_01',key.public_key(),'wire_01')
        audit=[]
        result=ReplayAdapter(registry,layer4=lambda i,v:audit.append(v),layer3=lambda w:None).run(
            tuple(ReplayRecord(payload=p,received_at=end) for p in payloads),as_of=end)
        statuses=tuple(v.status for v in result.events)
        if statuses!=expected or len(audit)!=len(payloads):
            raise RuntimeError(f'security scenario failed: {name}')
        summaries.append({'scenario':name,'statuses':statuses,'layer4_events':len(audit)})
    return summaries
