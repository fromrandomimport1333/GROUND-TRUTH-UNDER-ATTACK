"""Explicit local trust administration; never resets existing receiver state."""
import argparse
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from core.provenance.keyregistry import KeyRegistry
from core.secure_files import read_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('local/receiver.sqlite3'))
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--bundle', type=Path, default=Path('local/sensors'))
    revoke = commands.add_parser('revoke')
    revoke.add_argument('--sensor', required=True)
    revoke.add_argument('--key-id', required=True)
    enroll = commands.add_parser('enroll')
    enroll.add_argument('--sensor', required=True)
    enroll.add_argument('--public-key', type=Path, required=True)
    enroll.add_argument('--stream', required=True)
    enroll.add_argument('--retire-key')
    args = parser.parse_args()
    if args.command == 'init':
        KeyRegistry.import_bundle(args.state, args.bundle)
        print('KEY REGISTRY READY; retain receiver database across restarts')
    elif args.command == 'revoke':
        KeyRegistry(args.state).revoke(args.sensor, args.key_id)
        print('KEY REVOKED')
    else:
        key = serialization.load_pem_public_key(read_private(args.public_key))
        kid = KeyRegistry(args.state).enroll(args.sensor, key, args.stream, retire_key=args.retire_key)
        print('KEY ENROLLED:', kid)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
