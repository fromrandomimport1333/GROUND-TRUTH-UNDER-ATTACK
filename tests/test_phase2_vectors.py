"""Fixed public test vectors, never sensor deployment keys."""

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.contracts import SignedReading
from core.provenance.codec import decode_reading, encode_reading, record_hash, signing_bytes


def test_rfc8032_empty_message_known_answer():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    key = Ed25519PrivateKey.from_private_bytes(seed)
    assert key.public_key().public_bytes_raw().hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    assert key.sign(b"").hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


def test_fixed_cis_protocol_bytes_signature_and_record_hash():
    vector = json.loads((Path(__file__).parent / "vectors/reading_v1.json").read_text())
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(vector["seed_hex"]))
    reading = SignedReading.model_validate(vector["reading"])
    assert signing_bytes(reading).hex() == vector["signing_bytes_hex"]
    assert encode_reading(reading).hex() == vector["wire_cbor_hex"]
    assert record_hash(reading).hex() == vector["record_hash_hex"]
    assert key.sign(signing_bytes(reading)) == reading.signature
    key.public_key().verify(reading.signature, bytes.fromhex(vector["signing_bytes_hex"]))
    assert decode_reading(bytes.fromhex(vector["wire_cbor_hex"])) == reading
