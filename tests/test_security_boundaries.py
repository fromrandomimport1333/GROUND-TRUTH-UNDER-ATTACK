"""Regression tests for evidence retention and analytical input boundaries."""

import pytest
from pydantic import ValidationError

from core.contracts import (
    FeatureValue, FeatureWindow, ProcessedReading, ReferenceObservation,
    SignedReading, VerifiedReading,
)


def test_signature_success_does_not_imply_a_physical_range_filter(reading_data):
    raw = SignedReading(**{**reading_data, "value": 999999.0})
    event = VerifiedReading(schema_version="1.0", reading=raw, status="valid",
                            signature_verified=True, checked_at="2026-10-19T12:00:02Z")
    assert event.reading.value == 999999.0
    assert not ProcessedReading(provenance=event).ready_for_analysis


def test_bad_signature_evidence_can_reach_window_without_analytical_value(window_data, reading):
    event = VerifiedReading(schema_version="1.0", reading=reading, status="bad_signature",
                            signature_verified=False, checked_at="2026-10-19T12:00:02Z")
    item = ProcessedReading(provenance=event)
    window = FeatureWindow(**{**window_data, "readings": (item,)})
    assert window.readings[0].provenance.status == "bad_signature"
    assert not window.readings[0].ready_for_analysis


def test_malformed_events_cannot_be_misrepresented_as_sensor_observations(window_data):
    event = VerifiedReading(schema_version="1.0", reading=None, status="malformed",
                            signature_verified=None, checked_at="2026-10-19T12:00:02Z",
                            raw_message_sha256=bytes(32))
    with pytest.raises(ValidationError):
        FeatureWindow(**{**window_data, "readings": (ProcessedReading(provenance=event),)})


def test_instances_are_revalidated_after_an_unsafe_pydantic_copy(reading, verified):
    # Demonstrates why application code must validate trust-boundary inputs.
    invalid = reading.model_copy(update={"sequence": -1})
    with pytest.raises(ValidationError):
        VerifiedReading.model_validate({**verified.model_dump(), "reading": invalid})


def test_nested_json_unknown_field_is_rejected(window_data):
    window = FeatureWindow(**window_data)
    payload = window.model_dump(mode="json")
    payload["readings"][0]["provenance"]["reading"]["attack_label"] = "spike"
    import json
    with pytest.raises(ValidationError):
        FeatureWindow.model_validate_json(json.dumps(payload))


def test_distinct_sensor_streams_may_share_sequence_numbers(window_data, reading_data, processed):
    other = SignedReading(**{**reading_data, "sensor_id": "sensor_02"})
    event = VerifiedReading(schema_version="1.0", reading=other, status="valid",
                            signature_verified=True, checked_at="2026-10-19T12:00:02Z")
    second = ProcessedReading(provenance=event, normalized_value=25.5, normalized_unit="C")
    assert len(FeatureWindow(**{**window_data, "readings": (processed, second)}).readings) == 2


@pytest.mark.parametrize("value", [True, 3, "3.0", float("nan"), float("inf")])
def test_feature_values_are_finite_floats(value):
    with pytest.raises(ValidationError):
        FeatureValue(name="mean", value=value)


def test_available_reference_data_can_roundtrip(window_data):
    reference = ReferenceObservation(source_id="synthetic_reference",
                                     observed_at="2026-10-19T11:00:00Z",
                                     available_at="2026-10-19T11:01:00Z",
                                     quantity="temperature", value=24.0, unit="C")
    window = FeatureWindow(**window_data, reference_data=(reference,))
    assert FeatureWindow.model_validate_json(window.model_dump_json()) == window


def test_processing_preserves_raw_value_unit_and_signed_bytes(reading_data):
    raw = SignedReading(**{**reading_data, "value": 77.0, "unit": "F"})
    before = raw.model_dump_json()
    event = VerifiedReading(schema_version="1.0", reading=raw, status="valid",
                            signature_verified=True, checked_at="2026-10-19T12:00:02Z")
    # A synthetic derived value, not a call to an implemented normalization engine.
    derived = ProcessedReading(provenance=event, normalized_value=25.0, normalized_unit="C")
    assert derived.provenance.reading.model_dump_json() == before
    assert derived.provenance.reading.unit == "F"
