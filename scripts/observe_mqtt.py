"""Diagnostic CBOR observer; does NOT provide Layer 1b provenance verdicts."""

import argparse
import json
from pathlib import Path
import queue
import time

from adapters.mqtt_transport import MQTTConnection
from core.local_config import connection_settings, load_manifest
from core.provenance.codec import decode_reading


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, default=Path("local"))
    parser.add_argument("--count", type=int, default=9)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    if args.count < 1 or not 0 < args.timeout <= 3600:
        parser.error("positive bounded count/timeout required")
    manifest = load_manifest(args.local)
    incoming: queue.Queue = queue.Queue(maxsize=128)
    overflow = []
    connection = MQTTConnection(connection_settings(args.local, manifest, "observer"))

    def on_message(client, userdata, message):
        try:
            incoming.put_nowait((message.topic, message.payload))
        except queue.Full:
            # No blocking callback or silently unbounded diagnostic queue.
            if not overflow:
                overflow.append(True)

    connection.client.on_message = on_message
    deadline = time.monotonic() + args.timeout
    with connection:
        connection.subscribe("sensors/raw/+")
        print("OBSERVER_READY (decoding only; use scripts.receive_mqtt for verification)", flush=True)
        for _ in range(args.count):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("observer did not receive the requested number of messages")
            try:
                topic, payload = incoming.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("observer reception timed out") from exc
            reading = decode_reading(payload)
            if topic != f"sensors/raw/{reading.sensor_id}":
                raise ValueError("topic does not match envelope sensor identity")
            print(json.dumps({"event": "RECEIVED_CBOR", "sensor_id": reading.sensor_id,
                              "sequence": reading.sequence, "value": reading.value,
                              "signature_bytes": len(reading.signature), "bytes": len(payload),
                              "provenance_verified": False}), flush=True)
        if overflow:
            raise BufferError("observer queue overflowed; delivery is incomplete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
