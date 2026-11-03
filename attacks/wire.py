"""Explicit wire attacks for local tests. Production signing paths are unchanged."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from core.contracts import SignedReading
from core.provenance.codec import decode_reading, encode_reading, signing_bytes
from core.provenance.keys import key_id


def tamper_temperature(payload: bytes, value: float) -> bytes:
    reading = decode_reading(payload)
    forged = SignedReading(**{**reading.model_dump(), 'value': value})
    return encode_reading(forged)


def break_chain(payload: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """Re-sign a deliberately wrong predecessor with a compromised test key."""
    reading = decode_reading(payload)
    if key_id(private_key) != reading.key_id:
        raise ValueError('test key must match the signed identity')
    previous = bytes([reading.prev_hash[0] ^ 1]) + reading.prev_hash[1:]
    unsigned = SignedReading(**{**reading.model_dump(), 'prev_hash': previous})
    forged = SignedReading(**{**unsigned.model_dump(), 'signature': private_key.sign(signing_bytes(unsigned))})
    return encode_reading(forged)
