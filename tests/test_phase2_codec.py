import cbor2
import pytest

from core.provenance.codec import decode_reading, encode_reading, signing_bytes


def test_wire_roundtrip(reading):
    assert decode_reading(encode_reading(reading)) == reading


@pytest.mark.parametrize("payload", [b"", b"\xff", b"\xa1", b"x" * 65537, b"\x9f\xff",
    b"\xa2\x61a\x01\x61a\x02", b"\x81" * 20 + b"\x00", b"\xc0\x61x"])
def test_malformed_or_oversized_cbor_rejected(payload):
    with pytest.raises(ValueError):
        decode_reading(payload)


def test_trailing_content_rejected(reading):
    with pytest.raises(ValueError, match="trailing"):
        decode_reading(encode_reading(reading) + b"\x00")


def test_noncanonical_map_order_rejected(reading):
    data = cbor2.dumps(dict(reversed(list(reading.model_dump().items()))))
    assert data != encode_reading(reading)
    with pytest.raises(ValueError, match="canonical"):
        decode_reading(data)


def test_hex_strings_are_diagnostic_only(reading):
    data = reading.model_dump()
    data["signature"] = data["signature"].hex()
    with pytest.raises(ValueError, match="byte strings"):
        decode_reading(cbor2.dumps(data, canonical=True))


def test_signing_excludes_signature_but_chain_includes_it(reading):
    from core.contracts import SignedReading
    from core.provenance.codec import record_hash
    changed = SignedReading.model_validate({**reading.model_dump(), "signature": b"\xff" * 64})
    assert signing_bytes(changed) == signing_bytes(reading)
    assert record_hash(changed) != record_hash(reading)


@pytest.mark.parametrize("payload", [
    b"\xbb" + b"\xff" * 8,  # enormous declared map
    b"\x5b" + b"\xff" * 8,  # enormous declared bytestring
    b"\x9b" + b"\xff" * 8,  # arrays are outside the envelope subset
    b"\xd8\x1c\xa0",        # shared-reference semantic tag
    b"\xfb\x00",             # truncated float
])
def test_preflight_rejects_large_declarations_and_unsupported_types(payload):
    with pytest.raises(ValueError):
        decode_reading(payload)
