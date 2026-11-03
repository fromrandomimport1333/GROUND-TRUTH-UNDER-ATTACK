"""The six original Phase 4 semantic cases, plus defensive edge cases."""
import pytest
from pydantic import ValidationError
from core.contracts import SignedReading, VerifiedReading
from core.processing.validator import normalize_temperature, validate_reading


def test_celsius(verified):
    result = validate_reading(verified)
    assert result.normalized_value == 25.5 and result.normalized_unit == 'C'
    assert result.provenance == verified and not result.quality_flags


def test_fahrenheit():
    assert normalize_temperature(77.0, 'F').value_c == 25.0


def test_kelvin():
    assert normalize_temperature(298.15, 'K').value_c == pytest.approx(25.0)


def test_incorrect_type():
    assert normalize_temperature('25', 'C').flags == ('invalid_numeric_type',)


def test_null(verified):
    reading = SignedReading(**{**verified.reading.model_dump(), 'value': None})
    event = VerifiedReading(**{**verified.model_dump(), 'reading': reading})
    processed = validate_reading(event)
    assert processed.provenance.reading.value is None
    assert processed.normalized_value is None and 'missing_value' in processed.quality_flags


def test_unknown_unit(verified):
    reading = SignedReading(**{**verified.reading.model_dump(), 'unit': 'unknown'})
    event = VerifiedReading(**{**verified.model_dump(), 'reading': reading})
    result = validate_reading(event)
    assert result.normalized_value is None
    assert 'unknown_unit' in result.quality_flags and result.provenance.reading.unit == 'unknown'


@pytest.mark.parametrize('value', [True, 25, '25.0', {}, []])
def test_never_coerces_numeric_types(value):
    assert normalize_temperature(value, 'C').value_c is None


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_nonfinite_values(value):
    assert normalize_temperature(value, 'C').flags == ('nonfinite_value',)


@pytest.mark.parametrize('unit,value,expected', [('°C',0.0,0.0),('°F',32.0,0.0),
    ('K',0.0,-273.15),('F',-40.0,-40.0),('C',-273.15,-273.15),('F',-459.67,-273.15)])
def test_conversion_boundaries(unit, value, expected):
    assert normalize_temperature(value, unit).value_c == pytest.approx(expected)


@pytest.mark.parametrize('unit,value', [('K',-0.1), ('C',-300.0), ('F',-500.0)])
def test_impossible_temperature_is_not_clipped(unit, value):
    assert normalize_temperature(value, unit).flags == ('below_absolute_zero',)


def test_unknown_quantity():
    assert normalize_temperature(25.0,'C','humidity').flags == ('unsupported_quantity',)


def test_invalid_verdict_type_and_construct_bypass(verified):
    with pytest.raises(TypeError):
        validate_reading(verified.model_dump())
    forged = verified.model_copy(update={'reading': verified.reading.model_copy(update={'value': '25'})})
    with pytest.raises(ValidationError):
        validate_reading(forged)


def test_bad_provenance_never_normalized(verified):
    invalid = VerifiedReading(**{**verified.model_dump(), 'status':'bad_signature', 'signature_verified':False})
    result = validate_reading(invalid)
    assert not result.ready_for_analysis
    assert result.normalized_value is None and result.quality_flags == ('provenance_ineligible',)


def test_suspicious_but_possible_temperature_remains_evidence(verified):
    reading = SignedReading(**{**verified.reading.model_dump(), 'value': 1000.0})
    event = VerifiedReading(**{**verified.model_dump(), 'reading': reading})
    assert validate_reading(event).normalized_value == 1000.0
