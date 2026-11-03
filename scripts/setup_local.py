"""Provision a new private local demo bundle; never overwrite keys or state."""

import argparse
import json
import os
from pathlib import Path

from core.provenance.keys import key_password, safe_sensor_id
from core.provenance.pki import create_local_pki
from core.provenance.state import SignerState
from core.secure_files import new_private_directory, write_new_private
from generate_keys import generate_sensor_bundle


def setup_local(output: Path, password: bytes, *, port: int = 8883,
                sensor_ids: list[str] | None = None) -> Path:
    identifiers = sensor_ids if sensor_ids is not None else ["sensor_01", "sensor_02", "sensor_03"]
    if not 1024 <= port <= 65535 or len(password) < 16:
        raise ValueError("invalid port/password")
    if any(x in {"server", "observer"} for x in identifiers) or not 1 <= len(identifiers) <= 32:
        raise ValueError("invalid or reserved sensor identities")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate sensor identities")
    for identifier in identifiers:
        safe_sensor_id(identifier)
    root = new_private_directory(output)
    if any(c in str(root) for c in '\r\n"'):
        raise ValueError("configuration directory contains unsupported characters")
    sensors = generate_sensor_bundle(root / "sensors", identifiers, password)
    create_local_pki(root / "tls", identifiers, password)
    SignerState.create(root / "signer.sqlite3",
                       [(s["sensor_id"], s["key_id"], s["stream_id"]) for s in sensors])
    acl = "user observer\ntopic read sensors/raw/+\n\n"
    for identifier in identifiers:
        acl += f"user {identifier}\ntopic write sensors/raw/{identifier}\n\n"
    write_new_private(root / "acl.conf", acl.encode())
    # Mosquitto reads the rest of each path directive as a filename; do not add
    # shell quotes to these configuration values. Forward slashes work on Windows.
    config = (
        f"listener {port} 127.0.0.1\n"
        "allow_anonymous false\nrequire_certificate true\nuse_identity_as_username true\n"
        "tls_version tlsv1.2\npersistence false\nlog_dest stdout\n"
        "log_type error\nlog_type warning\nconnection_messages true\nmax_packet_size 65536\n"
        f"cafile {(root / 'tls/ca.pem').as_posix()}\n"
        f"certfile {(root / 'tls/server.pem').as_posix()}\n"
        f"keyfile {(root / 'tls/server.key.pem').as_posix()}\n"
        f"acl_file {(root / 'acl.conf').as_posix()}\n"
    )
    if os.name != "nt":
        import pwd
        # Foreground local broker uses the same account that owns the protected
        # TLS files. This avoids an implicit drop to a different unreadable user.
        config += f"user {pwd.getpwuid(os.getuid()).pw_name}\n"
    write_new_private(root / "mosquitto.conf", config.encode())
    manifest = {"schema_version": "1.0", "host": "127.0.0.1", "port": port, "sensors": sensors}
    write_new_private(root / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("local"))
    parser.add_argument("--port", type=int, default=8883)
    args = parser.parse_args()
    setup_local(args.output, key_password(), port=args.port)
    print("KEYS GENERATED; TLS IDENTITIES GENERATED; SIGNER STATE INITIALIZED")
    print("Start Mosquitto in the foreground with the generated mosquitto.conf.")
    print("Keep the private bundle in place; moving it requires regenerating broker paths.")


if __name__ == "__main__":
    main()
