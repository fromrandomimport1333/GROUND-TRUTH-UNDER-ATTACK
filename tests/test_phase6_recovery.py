from datetime import timedelta
import ssl
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from adapters.reconnect import ReconnectingPublisher, ReconnectingReceiver, RetryPolicy
from adapters.live_mqtt import TransportDisconnected
from core.contracts import SignedReading
from core.provenance.codec import encode_reading
from core.provenance.keys import key_id
from core.provenance.recovery import RecoveryArchive
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.keyregistry import KeyRegistry
from core.provenance.verifier import Verifier
from scripts.demo_attacks import synthetic_measurements
from sensor import flush_pending

START=synthetic_measurements()[0].observed_at


def signer(tmp_path):
    key=Ed25519PrivateKey.generate()
    identity=('sensor_01',key_id(key),'stream_01')
    state=SignerState.create(tmp_path/'sender.sqlite3',[identity])
    return SensorSigner(identity[0],identity[2],key,state)


def test_archive_survives_puback_and_sender_restart(tmp_path):
    s=signer(tmp_path);archive=RecoveryArchive.create(tmp_path/'archive.sqlite3',capacity=3)
    reading=s.sign(20.0,timestamp=START)
    sent=[]
    class Publisher:
        def publish(self,sensor,payload): sent.append(payload)
    assert flush_pending(s,Publisher(),archive)==1
    assert not s.state.pending(s.identity)
    assert RecoveryArchive(archive.path).records(s.identity,0,1)==(reading,)
    archive.retain(reading)
    assert archive.status()['records']==1
    with pytest.raises(ValueError): archive.records(s.identity,0,2)
    with pytest.raises(ValueError): archive.retain(SignedReading(**{**reading.model_dump(),'value':99.0}))
    reopened=SensorSigner(*s.identity[::2],s.private_key,SignerState(s.state.path))
    assert reopened.sign(21.0,timestamp=START+timedelta(seconds=1)).sequence==1


def test_full_archive_prevents_send_and_keeps_outbox(tmp_path):
    s=signer(tmp_path);archive=RecoveryArchive.create(tmp_path/'archive.sqlite3',capacity=1)
    first=s.sign(20.0,timestamp=START);archive.retain(first);s.state.acknowledge(first)
    second=s.sign(21.0,timestamp=START)
    class Publisher:
        def publish(self,*args): pytest.fail('record must not be sent when archive is full')
    with pytest.raises(BufferError): flush_pending(s,Publisher(),archive)
    assert s.state.pending(s.identity)==(second,)


def test_archive_recovery_does_not_bypass_freshness(tmp_path):
    s=signer(tmp_path);archive=RecoveryArchive.create(tmp_path/'archive.sqlite3')
    reading=s.sign(20.0,timestamp=START);archive.retain(reading)
    registry=KeyRegistry.create(tmp_path/'receiver.sqlite3')
    registry.enroll(s.identity[0],s.private_key.public_key(),s.identity[2])
    restored=archive.records(s.identity,0,1)[0]
    _,verdict=Verifier(registry,clock=lambda:START+timedelta(seconds=121)).verify(encode_reading(restored))
    assert verdict.status=='out_of_window'


def test_retry_same_payload_after_uncertain_delivery():
    observations=[];delays=[];instances=[]
    class Connection:
        def __init__(self,settings): self.number=len(instances);instances.append(self)
        def connect(self): pass
        def publish(self,sensor,payload):
            observations.append(payload)
            if self.number==0: raise TimeoutError('uncertain PUBACK')
        def close(self): pass
    publisher=ReconnectingPublisher(None,factory=Connection,sleep=delays.append)
    publisher.publish('sensor_01',b'exact signed bytes')
    assert observations==[b'exact signed bytes']*2 and delays==[.25]
    assert publisher.connection_attempts==2


@pytest.mark.parametrize('error',[PermissionError('denied'),ssl.SSLCertVerificationError('untrusted'),ValueError('invalid')])
def test_authentication_and_validation_failures_not_retried(error):
    class Connection:
        def __init__(self,s): pass
        def connect(self): raise error
        def close(self): pass
    publisher=ReconnectingPublisher(None,factory=Connection,sleep=lambda s:pytest.fail('must not retry'))
    with pytest.raises(type(error)): publisher.connect()
    assert publisher.connection_attempts==1


def test_retry_exhaustion_is_bounded():
    sleeps=[]
    class Connection:
        def __init__(self,s): pass
        def connect(self): raise ConnectionRefusedError('offline')
        def close(self): pass
    publisher=ReconnectingPublisher(None,factory=Connection,sleep=sleeps.append,policy=RetryPolicy(attempts=3))
    with pytest.raises(ConnectionRefusedError): publisher.publish('sensor',b'bytes')
    assert publisher.connection_attempts==3 and sleeps==[.25,.5]


def test_receiver_reconnects_only_transport_failures():
    instances=[]
    class Pipeline:
        def drain(self): return 0
    class Receiver:
        def __init__(self,*args): self.number=len(instances);instances.append(self)
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def receive(self,timeout):
            if self.number==0: raise TransportDisconnected('offline')
            return 'received'
    receiver=ReconnectingReceiver(None,Pipeline(),factory=Receiver,sleep=lambda s:None)
    assert receiver.receive()=='received' and receiver.connection_attempts==2
    class BadSink(Pipeline):
        def drain(self): raise ConnectionError('L4 failed')
    broken=ReconnectingReceiver(None,BadSink(),factory=Receiver,sleep=lambda s:pytest.fail('sink must not be retried as transport'))
    with pytest.raises(ConnectionError,match='L4 failed'): broken.__enter__()
    assert broken.connection_attempts==0


@pytest.mark.parametrize('kwargs',[{'attempts':True},{'attempts':0},{'initial_delay':float('inf')},{'initial_delay':3.0,'maximum_delay':1.0}])
def test_invalid_retry_bounds(kwargs):
    with pytest.raises(ValueError): RetryPolicy(**kwargs)


def test_idle_timeout_does_not_restart_receiver():
    class Pipeline:
        def drain(self): return 0
    class Receiver:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def receive(self,timeout): raise TimeoutError('idle')
    receiver=ReconnectingReceiver(None,Pipeline(),factory=Receiver,sleep=lambda s:pytest.fail('idle is not a disconnect'))
    with pytest.raises(TimeoutError,match='idle'): receiver.receive()
    assert receiver.connection_attempts==1
