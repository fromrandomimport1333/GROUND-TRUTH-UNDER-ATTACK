"""Synthetic internal-contract fixtures; not challenge dataset samples."""

from datetime import datetime, timezone

import pytest

from core.contracts import ProcessedReading, SignedReading, VerifiedReading


def pytest_addoption(parser):
    parser.addoption("--run-broker", action="store_true", default=False,
                     help="Run real local Mosquitto/mTLS tests; fail if broker is unavailable")
    parser.addoption("--broker-executable", default=None,
                     help="Path to Mosquitto executable for local transport tests")


@pytest.fixture
def reading_data():
    return dict(
        schema_version="1.0", sensor_id="sensor_01", key_id="key_01",
        stream_id="stream_01", timestamp="2026-10-19T12:00:01.000000Z",
        sequence=0, quantity="temperature", value=25.5, unit="C",
        prev_hash=bytes(32), signature=bytes(64),
    )


@pytest.fixture
def reading(reading_data):
    return SignedReading(**reading_data)


@pytest.fixture
def verified(reading):
    return VerifiedReading(
        schema_version="1.0", reading=reading, status="valid", signature_verified=True,
        checked_at=datetime(2026, 10, 19, 12, 0, 2, tzinfo=timezone.utc),
    )


@pytest.fixture
def processed(verified):
    return ProcessedReading(provenance=verified, normalized_value=25.5, normalized_unit="C")


@pytest.fixture
def window_data(processed):
    return dict(schema_version="1.0", window_id="window_01",
                start="2026-10-19T12:00:00Z", end="2026-10-19T12:01:00Z",
                as_of="2026-10-19T12:01:00Z", readings=(processed,))


def pytest_make_parametrize_id(config, val, argname):
    """Keep binary payloads out of node IDs and Windows environment variables."""
    if isinstance(val, bytes):
        import hashlib
        return f"{argname}_{len(val)}bytes_{hashlib.sha256(val).hexdigest()[:12]}"
    return None
