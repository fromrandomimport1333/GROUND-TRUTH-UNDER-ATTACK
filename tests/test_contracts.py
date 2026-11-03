import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.contracts import (
    FeatureGroup, FeatureValue, FeatureWindow, Location, ProcessedReading,
    ReferenceObservation, SignedReading, VerifiedReading,
)


@pytest.mark.parametrize("value", ["25.5", 25, True, False, float("nan"), float("inf"), -float("inf"), [], {}])
def test_invalid_raw_values_rejected(reading_data, value):
    reading_data["value"] = value
    with pytest.raises(ValidationError):
        SignedReading(**reading_data)


@pytest.mark.parametrize("value", [None, 0.0, 25.5, -999.0, 1000000.0])
def test_raw_values_preserve_missing_and_extremes(reading_data, value):
    reading_data["value"] = value
    assert SignedReading(**reading_data).value == value


@pytest.mark.parametrize("value", [-1, 2**63, True, "1", 1.5, None])
def test_bad_sequence(reading_data, value):
    reading_data["sequence"] = value
    with pytest.raises(ValidationError):
        SignedReading(**reading_data)


@pytest.mark.parametrize("sequence", [0, 2**63 - 1])
def test_sequence_boundaries(reading_data, sequence):
    reading_data["sequence"] = sequence
    assert SignedReading(**reading_data).sequence == sequence


@pytest.mark.parametrize("stamp", [
    "2026-10-19T12:00:00Z", "2026-10-19T12:00:00.000000", "2026-10-19T12:00:00.000000+00:00",
    "2026-02-30T12:00:00.000000Z", "2026-10-19T12:00:60.000000Z", 1234567890,
])
def test_signed_timestamp_has_one_unambiguous_form(reading_data, stamp):
    reading_data["timestamp"] = stamp
    with pytest.raises(ValidationError):
        SignedReading(**reading_data)


@pytest.mark.parametrize("field,value", [
    ("sensor_id", ""), ("sensor_id", "../keys"), ("sensor_id", "x" * 65),
    ("unit", ""), ("unit", "C\n"), ("key_id", 1), ("schema_version", "2.0"),
    ("signature", bytes(63)), ("signature", "ff" * 63),
    ("signature", "AA" * 64), ("prev_hash", bytes(31)), ("prev_hash", bytearray(32)),
])
def test_envelope_boundaries(reading_data, field, value):
    reading_data[field] = value
    with pytest.raises(ValidationError):
        SignedReading(**reading_data)


@pytest.mark.parametrize("field", [
    "schema_version", "sensor_id", "key_id", "stream_id", "timestamp", "sequence",
    "quantity", "value", "unit", "prev_hash", "signature",
])
def test_required_signed_fields(reading_data, field):
    del reading_data[field]
    with pytest.raises(ValidationError):
        SignedReading(**reading_data)


def test_unknown_fields_and_labels_forbidden(reading_data):
    for name in ("attack_label", "is_attack", "unexpected"):
        with pytest.raises(ValidationError):
            SignedReading(**reading_data, **{name: True})


def test_unknown_unit_is_preserved_for_later_semantic_validation(reading_data):
    reading_data["unit"] = "unknown-unit"
    assert SignedReading(**reading_data).unit == "unknown-unit"


def test_json_roundtrip_preserves_bytes_and_signed_timestamp(reading):
    encoded = reading.model_dump_json()
    payload = json.loads(encoded)
    assert payload["signature"] == "00" * 64
    assert payload["prev_hash"] == "00" * 32
    assert SignedReading.model_validate_json(encoded) == reading
    assert isinstance(reading.model_dump()["signature"], bytes)


def test_frozen_nested_contracts(reading, verified, processed, window_data):
    window = FeatureWindow(**window_data)
    for instance, field, value in (
        (reading, "value", 100.0), (verified, "status", "bad_signature"),
        (processed, "normalized_value", 100.0), (window, "readings", ()),
    ):
        with pytest.raises(ValidationError):
            setattr(instance, field, value)
    assert isinstance(window.readings, tuple)


@pytest.mark.parametrize("status,verdict", [
    ("valid", True), ("bad_signature", False), ("unknown_sensor", None),
    ("replay", True), ("gap", True), ("out_of_window", True),
    ("chain_anomaly", True), ("revoked_key", None),
])
def test_provenance_statuses(reading, status, verdict):
    event = VerifiedReading(schema_version="1.0", reading=reading, status=status,
                            signature_verified=verdict, checked_at="2026-10-19T12:00:02Z")
    assert event.analysis_eligible == (status == "valid")


@pytest.mark.parametrize("status,verdict", [
    ("valid", False), ("valid", None), ("bad_signature", True), ("unknown_sensor", True),
    ("replay", False), ("gap", None), ("out_of_window", None), ("chain_anomaly", False),
    ("revoked_key", True),
])
def test_inconsistent_provenance_rejected(reading, status, verdict):
    with pytest.raises(ValidationError):
        VerifiedReading(schema_version="1.0", reading=reading, status=status,
                        signature_verified=verdict, checked_at="2026-10-19T12:00:02Z")


def test_missing_value_is_not_analysis_eligible(reading_data):
    reading_data["value"] = None
    event = VerifiedReading(schema_version="1.0", reading=SignedReading(**reading_data),
                            status="valid", signature_verified=True, checked_at="2026-10-19T12:00:02Z")
    assert not event.analysis_eligible


def test_malformed_event_has_bounded_evidence():
    event = VerifiedReading(schema_version="1.0", reading=None, status="malformed",
                            signature_verified=None, checked_at="2026-10-19T12:00:02Z",
                            findings=("decode_error",), raw_message_sha256=bytes(32))
    assert not event.analysis_eligible
    assert VerifiedReading.model_validate_json(event.model_dump_json()) == event
    for changes in ({"raw_message_sha256": None}, {"signature_verified": False}, {"status": "valid"}):
        with pytest.raises(ValidationError):
            VerifiedReading.model_validate({**event.model_dump(), **changes})


def test_normalization_requires_value_and_unit_pair(verified):
    for data in ({"normalized_value": 2.0}, {"normalized_unit": "C"}):
        with pytest.raises(ValidationError):
            ProcessedReading(provenance=verified, **data)


def test_rejected_reading_retained_without_normalization(reading):
    event = VerifiedReading(schema_version="1.0", reading=reading, status="bad_signature",
                            signature_verified=False, checked_at="2026-10-19T12:00:02Z")
    assert ProcessedReading(provenance=event).provenance.reading == reading
    with pytest.raises(ValidationError):
        ProcessedReading(provenance=event, normalized_value=25.5, normalized_unit="C")


@pytest.mark.parametrize("start,end,as_of", [
    ("2026-10-19T12:01:00Z", "2026-10-19T12:01:00Z", "2026-10-19T12:01:00Z"),
    ("2026-10-19T12:01:00Z", "2026-10-19T12:00:00Z", "2026-10-19T12:01:00Z"),
    ("2026-10-19T12:00:00Z", "2026-10-19T12:01:00Z", "2026-10-19T12:00:59Z"),
])
def test_window_time_order(window_data, start, end, as_of):
    with pytest.raises(ValidationError):
        FeatureWindow(**{**window_data, "start": start, "end": end, "as_of": as_of})


def test_timezone_normalization_and_naive_rejection(window_data):
    window = FeatureWindow(**{**window_data, "start": "2026-10-19T13:00:00+01:00"})
    assert window.start == datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    for stamp in (datetime(2026, 10, 19, 12), "2026-10-19T12:00:00", 1000):
        with pytest.raises(ValidationError):
            FeatureWindow(**{**window_data, "start": stamp})


def test_window_excludes_endpoint_and_outside_readings(window_data):
    for changes in ({"end": "2026-10-19T12:00:01Z"}, {"start": "2026-10-19T12:00:02Z"}):
        with pytest.raises(ValidationError):
            FeatureWindow(**{**window_data, **changes})


def test_window_includes_start(window_data):
    assert FeatureWindow(**{**window_data, "start": "2026-10-19T12:00:01Z"}).readings


def test_window_rejects_duplicate_identity(window_data, processed):
    with pytest.raises(ValidationError):
        FeatureWindow(**{**window_data, "readings": (processed, processed)})


def test_window_rejects_reading_verified_after_cutoff(window_data, processed):
    provenance = VerifiedReading.model_validate({**processed.provenance.model_dump(),
                                                "checked_at": "2026-10-19T12:01:01Z"})
    item = ProcessedReading(provenance=provenance)
    with pytest.raises(ValidationError):
        FeatureWindow(**{**window_data, "readings": (item,)})


def test_feature_groups_show_absence_without_fake_features(window_data):
    window = FeatureWindow(**window_data)
    assert window.temporal_features.availability == "not_computed"
    assert window.spatial_features.values == ()
    group = FeatureGroup(availability="unavailable", reason="no_geography")
    assert group.values == ()


def test_feature_group_consistency():
    feature = FeatureValue(name="mean", value=2.0, unit="C")
    for data in (
        {"values": (feature,)}, {"availability": "available"},
        {"availability": "unavailable"},
        {"availability": "available", "values": (feature, feature)},
    ):
        with pytest.raises(ValidationError):
            FeatureGroup(**data)
    assert FeatureGroup(availability="available", values=(feature,)).values == (feature,)


@pytest.mark.parametrize("coordinates", [
    {"latitude": 91.0, "longitude": 0.0}, {"latitude": 0.0, "longitude": -181.0},
    {"latitude": float("nan"), "longitude": 0.0}, {"latitude": "36.0", "longitude": 10.0},
])
def test_invalid_locations(coordinates):
    with pytest.raises(ValidationError):
        Location(**coordinates)


@pytest.mark.parametrize("changes", [
    {"observed_at": "2026-10-19T12:01:00Z"}, {"available_at": "2026-10-19T12:01:01Z"},
])
def test_reference_causality_cutoffs(window_data, changes):
    data = dict(source_id="synthetic_reference", observed_at="2026-10-19T12:00:01Z",
                available_at="2026-10-19T12:00:02Z", quantity="temperature", value=24.0, unit="C")
    reference = ReferenceObservation(**{**data, **changes})
    with pytest.raises(ValidationError):
        FeatureWindow(**window_data, reference_data=(reference,))


def test_window_roundtrip_preserves_original_evidence(window_data):
    window = FeatureWindow(**window_data)
    restored = FeatureWindow.model_validate_json(window.model_dump_json())
    assert restored == window
    assert restored.readings[0].provenance.reading.value == 25.5


def test_labels_cannot_enter_detector_contract(window_data):
    with pytest.raises(ValidationError):
        FeatureWindow(**window_data, attack_label="spike")
