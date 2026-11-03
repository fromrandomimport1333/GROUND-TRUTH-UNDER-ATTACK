"""Multi-sensor Layer 1a simulator with durable signing and MQTT/TLS delivery."""

import argparse
import json
from pathlib import Path
import random
import time

from adapters.mqtt_transport import MQTTConnection
from adapters.reconnect import ReconnectingPublisher, RetryPolicy
from core.provenance.recovery import RecoveryArchive
from core.local_config import connection_settings, contained_file, load_manifest
from core.provenance.codec import encode_reading
from core.provenance.keys import key_id, key_password, load_private_key
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState


def flush_pending(signer: SensorSigner, connection: MQTTConnection, archive: RecoveryArchive | None = None) -> int:
    sent = 0
    for reading in signer.state.pending(signer.identity):
        if archive is not None:
            archive.retain(reading)
        connection.publish(reading.sensor_id, encode_reading(reading))
        signer.state.acknowledge(reading)
        sent += 1
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, default=Path("local"))
    parser.add_argument("--count", type=int, default=0, help="rounds per sensor; 0 runs until Ctrl+C")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--seed", type=int, help="repeatable synthetic values, not fixed cryptographic keys")
    parser.add_argument("--recovery-archive", type=Path, help="existing explicitly provisioned recovery archive")
    parser.add_argument("--reconnect-attempts", type=int, default=5)
    args = parser.parse_args()
    retry_policy = RetryPolicy(attempts=args.reconnect_attempts)
    archive = RecoveryArchive(args.recovery_archive) if args.recovery_archive else None
    if args.count < 0 or not 0 < args.interval <= 3600:
        parser.error("count must be nonnegative; interval must be in (0,3600]")
    manifest = load_manifest(args.local)
    password = key_password()
    state = SignerState(contained_file(args.local, "signer.sqlite3"))
    participants = []
    try:
        for item in manifest["sensors"]:
            key = load_private_key(contained_file(args.local, "sensors/" + item["private_key"]), password)
            if key_id(key) != item["key_id"]:
                raise ValueError("sensor key fingerprint does not match enrolled identity")
            signer = SensorSigner(item["sensor_id"], item["stream_id"], key, state)
            connection = ReconnectingPublisher(connection_settings(args.local, manifest, item["sensor_id"]), policy=retry_policy)
            generator = random.Random(f"{args.seed}:{item['sensor_id']}") if args.seed is not None else random.SystemRandom()
            participants.append((signer, connection, generator))
            connection.connect()
            recovered = flush_pending(signer, connection, archive)
            print(json.dumps({"event": "MQTT_TLS_CONNECTED", "sensor_id": item["sensor_id"],
                              "recovered_pending": recovered}), flush=True)
        print("MULTI-SENSOR RUNNING", flush=True)
        rounds = 0
        deadline = time.monotonic()
        while args.count == 0 or rounds < args.count:
            for signer, connection, generator in participants:
                reading = signer.sign(float(generator.uniform(20.0, 30.0)))
                flush_pending(signer, connection, archive)
                print(json.dumps({"event": "CBOR_MESSAGE_PUBLISHED", "sensor_id": reading.sensor_id,
                                  "sequence": reading.sequence, "value": reading.value,
                                  "unit": reading.unit, "timestamp": reading.timestamp}), flush=True)
            rounds += 1
            if args.count and rounds >= args.count:
                break
            deadline += args.interval
            time.sleep(max(0.0, deadline - time.monotonic()))
        return 0
    except KeyboardInterrupt:
        print("Stopped; signer state and uncertain publications are retained.")
        return 0
    finally:
        for _, connection, _ in participants:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
