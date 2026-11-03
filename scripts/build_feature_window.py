"""Build one bounded FeatureWindow from a trusted local receiver journal."""
import argparse
import json
from pathlib import Path
from datetime import datetime
from core.processing.engine import ProcessingEngine
from core.processing.journal import read_verifier_journal
from core.processing.policy import ProcessingPolicy
from core.provenance.keyregistry import KeyRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('local/receiver.sqlite3'))
    parser.add_argument('--start', required=True, help='aware ISO UTC window start')
    parser.add_argument('--end', required=True, help='aware ISO UTC window end')
    parser.add_argument('--as-of', required=True, help='aware ISO availability cutoff, at/after end')
    parser.add_argument('--window-seconds', type=int)
    parser.add_argument('--policy', type=Path, help='bounded local ProcessingPolicy JSON')
    args = parser.parse_args()
    if args.policy is not None:
        if args.window_seconds is not None:
            parser.error('set window_seconds inside the policy file when using --policy')
        with args.policy.open('rb') as source:
            data = source.read(65537)
        if len(data) > 65536:
            raise ValueError('processing policy exceeds 64 KiB')
        policy = ProcessingPolicy.model_validate_json(data)
    else:
        policy = ProcessingPolicy(window_seconds=args.window_seconds if args.window_seconds is not None else 60)
    result = ProcessingEngine(policy).build_window(read_verifier_journal(KeyRegistry(args.state)),
        start=datetime.fromisoformat(args.start), end=datetime.fromisoformat(args.end),
        as_of=datetime.fromisoformat(args.as_of))
    print(json.dumps({'policy': result.policy.model_dump(mode='json'),
                      'window': result.window.model_dump(mode='json'),
                      'excluded_events': [{'reason': x.reason, 'event': x.event.model_dump(mode='json')}
                                          for x in result.excluded_events]}, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
