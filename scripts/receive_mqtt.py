"""Run Layer 1b using the local observer TLS identity and a separate trust store."""
import argparse
import json
from pathlib import Path

from adapters.reconnect import ReconnectingReceiver, RetryPolicy
from core.local_config import connection_settings, load_manifest
from core.pipeline import ProvenancePipeline
from core.provenance.keyregistry import KeyRegistry
from core.provenance.replay_protection import ReplayPolicy
from core.provenance.verifier import Verifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', type=Path, default=Path('local'))
    parser.add_argument('--state', type=Path)
    parser.add_argument('--count', type=int, default=9)
    parser.add_argument('--timeout', type=float, default=60.0)
    parser.add_argument('--max-age', type=float, default=120.0)
    parser.add_argument('--future-skew', type=float, default=5.0)
    parser.add_argument("--reconnect-attempts", type=int, default=5)
    args = parser.parse_args()
    retry_policy = RetryPolicy(attempts=args.reconnect_attempts)
    if not 1 <= args.count <= 1000000 or not 0 < args.timeout <= 3600:
        parser.error('count and timeout must be positive and bounded')
    manifest = load_manifest(args.local)
    registry = KeyRegistry(args.state or args.local / 'receiver.sqlite3')
    verifier = Verifier(registry, policy=ReplayPolicy(args.max_age, args.future_skew))

    def layer4_sink(event_id, verdict):
        # Demonstration sink; replace with the team's idempotent L4 consumer.
        print(json.dumps({'event_id': event_id, 'provenance': verdict.model_dump(mode='json')}), flush=True)

    pipeline = ProvenancePipeline(verifier, layer4_sink)
    while pipeline.drain():
        pass
    with ReconnectingReceiver(connection_settings(args.local, manifest, 'observer'), pipeline, policy=retry_policy) as receiver:
        print('VERIFIER_READY', flush=True)
        for _ in range(args.count):
            receiver.receive(args.timeout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
