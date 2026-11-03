"""Provision, inspect or explicitly resend retained signed measurements."""
import argparse
import json
from pathlib import Path
from adapters.reconnect import ReconnectingPublisher
from core.local_config import load_manifest, connection_settings
from core.provenance.codec import encode_reading
from core.provenance.recovery import RecoveryArchive


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',required=True,type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create');create.add_argument('--capacity',type=int,default=10000)
    sub.add_parser('status')
    resend=sub.add_parser('resend')
    resend.add_argument('--local',type=Path,required=True)
    resend.add_argument('--sensor',required=True)
    resend.add_argument('--start',type=int,required=True)
    resend.add_argument('--stop',type=int,required=True)
    args=parser.parse_args()
    if args.command=='create':
        archive=RecoveryArchive.create(args.archive,capacity=args.capacity)
    else:
        archive=RecoveryArchive(args.archive)
    if args.command=='resend':
        manifest=load_manifest(args.local)
        item=next((s for s in manifest['sensors'] if s['sensor_id']==args.sensor),None)
        if item is None: parser.error('sensor absent from local bundle')
        records=archive.records((item['sensor_id'],item['key_id'],item['stream_id']),args.start,args.stop)
        publisher=ReconnectingPublisher(connection_settings(args.local,manifest,args.sensor))
        try:
            for reading in records: publisher.publish(reading.sensor_id,encode_reading(reading))
        finally:
            publisher.close()
        print(json.dumps({'resent':len(records),'receiver_acceptance_confirmed':False,
                          'note':'Original timestamps/signatures retained; inspect receiver verdicts.'}))
    else:
        print(json.dumps(archive.status()))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
