"""Isolated synthetic TLS/plaintext control. No plaintext production option."""
import queue
import threading
import time
import uuid
import paho.mqtt.client as mqtt
from adapters.mqtt_transport import MQTTConnection
from core.local_config import connection_settings
from eval.local_broker import LocalBroker
from eval.performance import measure, summarize_ns


class PlainLoopbackControl:
    """Evaluation-only client pinned to loopback and an owned lab broker port."""
    def __init__(self,port):
        self.port=port;self.ready=threading.Event();self.subscribed=threading.Event();self.denied=False
        self.client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='lab-'+uuid.uuid4().hex,protocol=mqtt.MQTTv5,reconnect_on_failure=False)
        self.client.on_connect=self._connected
        self.client.on_subscribe=self._subscribed
        self.started=False
    def _connected(self,client,userdata,flags,reason,properties):
        self.denied=reason.is_failure;self.ready.set()
    def _subscribed(self,client,userdata,mid,reasons,properties):
        self.denied=any(r.is_failure for r in reasons);self.subscribed.set()
    def connect(self):
        self.client.connect('127.0.0.1',self.port,30);self.client.loop_start();self.started=True
        if not self.ready.wait(5) or self.denied: raise ConnectionError('control connect failed')
    def subscribe(self,topic):
        self.client.subscribe(topic,qos=1)
        if not self.subscribed.wait(5) or self.denied: raise ConnectionError('control subscription failed')
    def publish(self,sensor,payload):
        info=self.client.publish('sensors/raw/'+sensor,payload,qos=1,retain=False)
        if info.rc!=mqtt.MQTT_ERR_SUCCESS: raise ConnectionError('control publish enqueue failed')
        info.wait_for_publish(timeout=5)
        if not info.is_published(): raise TimeoutError('control PUBACK timed out')
    def close(self):
        self.client.disconnect()
        if self.started: self.client.loop_stop();self.started=False


def run_transport_benchmark(root,executable,payload, *, iterations,series,warmup):
    report={}
    with LocalBroker(root,executable,plaintext_control=True) as broker:
        for mode in ('mtls','plaintext_loopback_control'):
            def make(role):
                if mode=='mtls': return MQTTConnection(connection_settings(broker.root,broker.manifest,role))
                return PlainLoopbackControl(broker.plain_port)
            def handshake():
                connection=make('sensor_01')
                try: connection.connect()
                finally: connection.close()
            setup=measure(handshake,iterations=min(iterations,5),series=series,warmup=min(warmup,2))
            incoming=queue.Queue(maxsize=16)
            observer=make('observer');publisher=make('sensor_01')
            observer.client.on_message=lambda c,u,m:incoming.put_nowait((bytes(m.payload),time.perf_counter_ns()))
            ack_groups=[];delivery_groups=[];acks=[];deliveries=[]
            try:
                observer.connect();observer.subscribe('sensors/raw/+');publisher.connect()
                for group in range(series+1):
                    ga=[];gd=[]
                    for _ in range(warmup if group==0 else iterations):
                        started=time.perf_counter_ns();publisher.publish('sensor_01',payload)
                        acknowledged=time.perf_counter_ns()
                        received,arrived=incoming.get(timeout=5)
                        if received!=payload: raise RuntimeError('transport benchmark payload mismatch')
                        ga.append(acknowledged-started);gd.append(arrived-started)
                    if group:
                        ack_groups.append(summarize_ns(ga));delivery_groups.append(summarize_ns(gd));acks.extend(ga);deliveries.extend(gd)
            finally:
                publisher.close();observer.close()
            report[mode]={'connection_setup_to_connack':setup,
                'publish_to_broker_puback':{'series':ack_groups,'pooled':summarize_ns(acks)},
                'publish_to_subscriber_callback':{'series':delivery_groups,'pooled':summarize_ns(deliveries)}}
    report['comparison']={}
    for stage in ('connection_setup_to_connack','publish_to_broker_puback','publish_to_subscriber_callback'):
        secure=report['mtls'][stage]['pooled']['mean_ms'];plain=report['plaintext_loopback_control'][stage]['pooled']['mean_ms']
        report['comparison'][stage]={'mtls_minus_plain_mean_ms':secure-plain,'mtls_to_plain_ratio':secure/plain if plain else None}
    report['scope']='Owned loopback broker, synthetic identical CBOR payload, QoS 1. Callback latency is transport-only, not verification. Plaintext is evaluation-only and lacks equivalent authentication. TLS setup includes certificate/context loading and CONNACK. Sequential modes are not randomized causal estimates; small differences can be noise.'
    return report
