"""Offline Layer 1a demonstration using temporary synthetic keys and readings."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.contracts import SignedReading
from core.provenance.codec import decode_reading, encode_reading, record_hash, signing_bytes
from core.provenance.keys import key_id
from core.provenance.signer import SensorSigner
from core.provenance.state import SignerState


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cis-signing-") as temporary:
        keys = {f"sensor_{i:02d}": Ed25519PrivateKey.generate() for i in range(1, 4)}
        state = SignerState.create(Path(temporary) / "state.sqlite3",
                                  [(sensor, key_id(key), "stream_01") for sensor, key in keys.items()])
        for sensor, key in keys.items():
            signer = SensorSigner(sensor, "stream_01", key, state)
            history = []
            for index in range(3):
                reading = signer.sign(25.0 + index, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
                                      + timedelta(seconds=2 * index))
                key.public_key().verify(reading.signature, signing_bytes(reading))
                assert decode_reading(encode_reading(reading)) == reading
                if history:
                    assert reading.prev_hash == record_hash(history[-1])
                history.append(reading)
            changed = SignedReading.model_validate({**history[0].model_dump(), "value": 999.0})
            try:
                key.public_key().verify(changed.signature, signing_bytes(changed))
            except InvalidSignature:
                pass
            else:
                raise AssertionError("tampering was not detected")
            assert history[1].prev_hash != record_hash(changed)
            print(f"{sensor}: 3 SIGNED; CBOR ROUNDTRIP OK; CHAIN CREATED; TAMPERING CHECK OK")
        print("OFFLINE L1a DEMO PASSED; no broker or Layer 1b verifier was used.")


if __name__ == "__main__":
    main()
