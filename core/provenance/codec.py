"""Version 1 canonical CBOR and domain-separated signature/chain bytes."""

from __future__ import annotations

import hashlib
from io import BytesIO

import cbor2

from core.contracts import SignedReading

SIGNING_DOMAIN = b"CIS/reading/v1\0"
CHAIN_DOMAIN = b"CIS/chain/v1\0"
GENESIS_HASH = bytes(32)
MAX_PAYLOAD_BYTES = 65536


def _preflight(data: bytes) -> None:
    """Reject oversized declarations/tags/arrays before a general decoder allocates.

    The v1 envelope needs only bounded maps, strings, bytes, unsigned integers,
    floats and null. Other CBOR types have no role in this wire contract.
    """
    def item(offset: int, depth: int) -> int:
        if depth > 8 or offset >= len(data):
            raise ValueError("truncated or overly nested CBOR")
        head = data[offset]
        major, additional = head >> 5, head & 31
        offset += 1
        if additional < 24:
            argument = additional
        elif additional in (24, 25, 26, 27):
            width = 1 << (additional - 24)
            if offset + width > len(data):
                raise ValueError("truncated CBOR length/value")
            argument = int.from_bytes(data[offset:offset + width], "big")
            offset += width
        else:
            raise ValueError("indefinite or reserved CBOR encoding is forbidden")
        if major == 0:
            if argument > 2**63 - 1:
                raise ValueError("integer exceeds v1 range")
            return offset
        if major in (2, 3):
            if argument > 512 or offset + argument > len(data):
                raise ValueError("CBOR string declaration exceeds v1 bounds")
            return offset + argument
        if major == 5:
            if argument > 32:
                raise ValueError("CBOR map declaration exceeds v1 bounds")
            for _ in range(argument * 2):
                offset = item(offset, depth + 1)
            return offset
        if major == 7 and additional in (22, 25, 26, 27):
            return offset
        raise ValueError("CBOR type is outside the v1 envelope subset")

    if item(0, 0) != len(data):
        raise ValueError("trailing CBOR content")


def signing_bytes(reading: SignedReading) -> bytes:
    checked = SignedReading.model_validate(reading)
    return SIGNING_DOMAIN + cbor2.dumps(checked.model_dump(exclude={"signature"}), canonical=True)


def encode_reading(reading: SignedReading) -> bytes:
    data = cbor2.dumps(SignedReading.model_validate(reading).model_dump(), canonical=True)
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("reading exceeds transport limit")
    return data


def record_hash(reading: SignedReading) -> bytes:
    return hashlib.sha256(CHAIN_DOMAIN + encode_reading(reading)).digest()


def decode_reading(data: bytes) -> SignedReading:
    """Bounded structural decoder; the separate verifier makes provenance verdicts."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PAYLOAD_BYTES:
        raise ValueError("invalid payload size/type")
    _preflight(data)
    stream = BytesIO(data)
    try:
        decoder = cbor2.CBORDecoder(stream, max_depth=8, allow_indefinite=False,
                                   allow_duplicate_keys=False)
        payload = decoder.decode()
        if stream.read(1):
            raise ValueError("trailing CBOR content")
        if type(payload) is not dict or any(type(k) is not str for k in payload):
            raise ValueError("expected a string-keyed CBOR map")
        if type(payload.get("signature")) is not bytes or type(payload.get("prev_hash")) is not bytes:
            raise ValueError("wire signatures and hashes must be native CBOR byte strings")
        reading = SignedReading.model_validate(payload)
        if encode_reading(reading) != data:
            raise ValueError("wire encoding is not the exact canonical v1 representation")
        return reading
    except (cbor2.CBORDecodeError, RecursionError, TypeError, OverflowError) as exc:
        raise ValueError("malformed CBOR payload") from exc
