"""Layer 1a signing; paired with the separate Layer 1b verifier."""

from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.contracts import Location, SignedReading
from core.provenance.codec import signing_bytes
from core.provenance.keys import key_id
from core.provenance.state import SignerState


class SensorSigner:
    def __init__(self, sensor_id: str, stream_id: str, private_key: Ed25519PrivateKey,
                 state: SignerState):
        self.private_key = private_key
        self.identity = (sensor_id, key_id(private_key), stream_id)
        self.state = state

    def sign(self, value: float | None, *, unit: str = "C", quantity: str = "temperature",
             timestamp: datetime | None = None, location: Location | None = None) -> SignedReading:
        stamp = timestamp if timestamp is not None else datetime.now(timezone.utc)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone-aware sensor timestamp required")
        text = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        def create(sequence: int, previous: bytes) -> SignedReading:
            data = dict(schema_version="1.0", sensor_id=self.identity[0], key_id=self.identity[1],
                        stream_id=self.identity[2], timestamp=text, sequence=sequence,
                        quantity=quantity, value=value, unit=unit, location=location,
                        prev_hash=previous, signature=bytes(64))
            unsigned = SignedReading(**data)
            signature = self.private_key.sign(signing_bytes(unsigned))
            return SignedReading(**{**data, "signature": signature})

        return self.state.issue(self.identity, create)
