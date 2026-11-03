"""Owned three-sensor mTLS soak with controlled failures and retained-byte recovery."""
import argparse
from collections import Counter
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time

from adapters.reconnect import ReconnectingPublisher, ReconnectingReceiver
from core.local_config import connection_settings
from core.pipeline import ProvenancePipeline
from core.processing.engine import ProcessingEngine
from core.processing.journal import read_verifier_journal
from core.provenance.codec import encode_reading
from core.provenance.keyregistry import KeyRegistry
from core.provenance.keys import load_private_key
from core.provenance.recovery import RecoveryArchive
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState
from core.provenance.verifier import Verifier
from eval.local_broker import LocalBroker
from eval.performance import memory_snapshot
from sensor import flush_pending


def run_stability(root,executable, *, duration=60.0, interval=0.2, max_rounds=2000):
    import math
    if any(type(x) not in (float,int) or not math.isfinite(x) for x in (duration,interval)) or not 0<duration<=3600 or not 0<interval<=10:
        raise ValueError('duration must be (0,3600], interval (0,10]')
    if type(max_rounds) is not int or not 1<=max_rounds<=2000:
        raise ValueError('max_rounds must be in [1,2000]')
    with LocalBroker(root,executable) as broker:
        manifest=broker.manifest
        registry=KeyRegistry.import_bundle(root/'receiver.sqlite3',root/'sensors')
        archive=RecoveryArchive.create(root/'recovery.sqlite3',capacity=max_rounds*3+10)
        counts=Counter();faults=[];fail_next=[False]
        def sink(event_id,verdict):
            if fail_next[0]:
                fail_next[0]=False
                raise RuntimeError('controlled_l4_interruption')
            counts[verdict.status]+=1
        def new_receiver():
            pipeline=ProvenancePipeline(Verifier(KeyRegistry(registry.path)),sink)
            receiver=ReconnectingReceiver(connection_settings(root,manifest,'observer'),pipeline)
            receiver.__enter__()
            return receiver
        state=SignerState(root/'signer.sqlite3')
        keys=[load_private_key(root/'sensors'/s['private_key'],broker.password) for s in manifest['sensors']]
        def make_signers():
            return [SensorSigner(s['sensor_id'],s['stream_id'],key,SignerState(state.path)) for s,key in zip(manifest['sensors'],keys)]
        signers=make_signers()
        publishers=[ReconnectingPublisher(connection_settings(root,manifest,s['sensor_id'])) for s in manifest['sensors']]
        receiver=None;issued=0;rounds=0;memory=[]
        start=time.monotonic();cpu_start=time.process_time()
        try:
            receiver=new_receiver()
            with ThreadPoolExecutor(max_workers=3) as pool:
                while rounds<max_rounds and time.monotonic()-start<duration:
                    if rounds==2:
                        receiver.close();receiver=None
                    if rounds==4:
                        receiver.close();receiver=None
                        broker.stop();broker.start()
                        receiver=new_receiver()
                        faults.append('owned_broker_restart')
                    if rounds==10:
                        signers=make_signers()
                        faults.append('signer_state_reopened')
                    def send(index):
                        signer=signers[index]
                        reading=signer.sign(20.0+index+rounds*.001)
                        flush_pending(signer,publishers[index],archive)
                        return reading
                    readings=list(pool.map(send,range(3)));issued+=3
                    if rounds==2:
                        # Broker accepted these while the clean-session observer was offline.
                        receiver=new_receiver()
                        later=send(0);issued+=1
                        if receiver.receive(5).status!='gap': raise RuntimeError('missing predecessor was not reported')
                        for index,reading in enumerate(readings):
                            retained=archive.records(reading.identity[:3],reading.sequence,reading.sequence+1)[0]
                            publishers[index].publish(reading.sensor_id,encode_reading(retained))
                            if receiver.receive(5).status!='valid': raise RuntimeError('retained predecessor recovery failed')
                        publishers[0].publish(later.sensor_id,encode_reading(later))
                        if receiver.receive(5).status!='valid': raise RuntimeError('recovery did not restore continuity')
                        faults.append('observer_outage_gap_and_archive_recovery')
                    elif rounds==6:
                        fail_next[0]=True
                        try:
                            receiver.receive(5)
                            raise AssertionError('controlled sink failure did not occur')
                        except RuntimeError as error:
                            if str(error)!='controlled_l4_interruption': raise
                        receiver.close();receiver=new_receiver()
                        # Drain recovered the durable first event; retransmit all three
                        # because callbacks/clean-session buffering may have lost the rest.
                        statuses=[]
                        for index,reading in enumerate(readings):
                            publishers[index].publish(reading.sensor_id,encode_reading(reading))
                            statuses.append(receiver.receive(5).status)
                        if Counter(statuses)!=Counter({'replay':1,'valid':2}):
                            raise RuntimeError('receiver restart recovery mismatch')
                        faults.append('durable_l4_event_recovery_and_receiver_restart')
                    else:
                        for _ in readings:
                            if receiver.receive(5).status!='valid': raise RuntimeError('unexpected normal provenance status')
                    if rounds==8:
                        publishers[0].publish(readings[0].sensor_id,encode_reading(readings[0]))
                        if receiver.receive(5).status!='replay': raise RuntimeError('duplicate was not rejected')
                        faults.append('duplicate_delivery')
                    rounds+=1
                    if rounds==1 or rounds%25==0:
                        memory.append({'elapsed_seconds':time.monotonic()-start,**memory_snapshot()})
                    if rounds<max_rounds:
                        time.sleep(min(interval,max(0,duration-(time.monotonic()-start))))
            # Observe state consistency rather than inferring delivery from PUBACK.
            with registry.transaction() as db:
                heads={(s,k,t):n for s,k,t,n in db.execute('SELECT sensor,key_id,stream,next_sequence FROM heads')}
            with closing(state._connection()) as db:
                sender_heads={(s,k,t):n for s,k,t,n in db.execute('SELECT sensor,key_id,stream,next_sequence FROM streams')}
            if heads!=sender_heads or counts['valid']!=issued:
                raise RuntimeError('sender and receiver sequence heads diverged')
            if any(state.pending(s.identity) for s in signers) or Verifier(registry).pending():
                raise RuntimeError('durable delivery remained pending')
            events=read_verifier_journal(registry)
            windows,unassigned=ProcessingEngine().build_closed_windows(events,as_of=datetime.now(timezone.utc))
            elapsed=time.monotonic()-start;cpu=time.process_time()-cpu_start
            return {'schema_version':'1.0','kind':'phase6_stability','source':'synthetic',
                'requested_duration_seconds':duration,'elapsed_seconds':elapsed,'rounds':rounds,'max_rounds':max_rounds,
                'duration_target_reached':elapsed>=duration,'round_limit_reached':rounds==max_rounds,
                'sensors':3,'issued_records':issued,'audited_status_counts':dict(counts),
                'faults_exercised':faults,'all_planned_faults_exercised':len(faults)==5,
                'sender_receiver_heads_match':True,'pending_sender_records':0,'pending_receiver_events':0,
                'archive':archive.status(),'closed_feature_windows':len(windows),'unassigned_events':len(unassigned),
                'publisher_connection_attempts':[p.connection_attempts for p in publishers],
                'process_cpu_seconds':cpu,'cpu_percent_one_core':100*cpu/elapsed if elapsed else None,
                'memory_samples':memory,'memory_final':memory_snapshot(),
                'sqlite_bytes':{name:(root/name).stat().st_size for name in ('signer.sqlite3','receiver.sqlite3','recovery.sqlite3')},
                'limitations':['Bounded local soak; not proof of indefinite uptime.',
                    'CPU and RSS describe Python, excluding the broker process.',
                    'Open event-time windows remain unassigned until closed.',
                    'Delivered receiver journal and archive grow; archive has a hard capacity and no automatic deletion.',
                    'Freshness policy stays active; long outages may require explicitly enrolling a new stream.']}
        finally:
            if receiver is not None: receiver.close()
            for publisher in publishers: publisher.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--broker-executable',required=True,type=Path)
    parser.add_argument('--duration',type=float,default=60.0);parser.add_argument('--interval',type=float,default=.2)
    parser.add_argument('--max-rounds',type=int,default=2000);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='cis-stability-') as directory:
        report=run_stability(Path(directory)/'local',args.broker_executable,duration=args.duration,interval=args.interval,max_rounds=args.max_rounds)
    text=json.dumps(report,indent=2,allow_nan=False)+'\n'
    if args.output:
        with args.output.open('x',encoding='utf-8') as output: output.write(text)
    print(text,end='')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
