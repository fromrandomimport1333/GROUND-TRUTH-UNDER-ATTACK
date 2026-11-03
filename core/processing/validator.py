"""Temperature semantics after provenance verification; raw evidence is immutable."""
from dataclasses import dataclass
import math

from core.contracts import ProcessedReading, VerifiedReading


@dataclass(frozen=True)
class TemperatureResult:
    value_c: float | None
    flags: tuple[str, ...]


def normalize_temperature(value: object, unit: object, quantity: object = 'temperature') -> TemperatureResult:
    """Never coerce strings, integers or booleans at the signed-data boundary."""
    flags = []
    if quantity != 'temperature':
        flags.append('unsupported_quantity')
    if type(unit) is not str or unit not in ('C', '°C', 'F', '°F', 'K'):
        flags.append('unknown_unit')
    if value is None:
        flags.append('missing_value')
    elif type(value) is not float:
        flags.append('invalid_numeric_type')
    elif not math.isfinite(value):
        flags.append('nonfinite_value')
    if flags:
        return TemperatureResult(None, tuple(flags))
    minimum = -273.15 if unit in ('C', '°C') else -459.67 if unit in ('F', '°F') else 0.0
    if value < minimum:
        return TemperatureResult(None, ('below_absolute_zero',))
    if unit in ('C', '°C'):
        converted = value
    elif unit in ('F', '°F'):
        converted = (value - 32.0) * (5.0 / 9.0)
    else:
        converted = value - 273.15
    if not math.isfinite(converted):
        return TemperatureResult(None, ('normalization_overflow',))
    # Input-unit physical bounds were checked first. Canonicalize only binary
    # roundoff at the absolute-zero conversion boundary (e.g. -459.67 F).
    if converted < -273.15:
        converted = -273.15
    return TemperatureResult(float(converted), ())


def validate_reading(event: VerifiedReading) -> ProcessedReading:
    if not isinstance(event, VerifiedReading):
        raise TypeError('expected an internal VerifiedReading from Layer 1b')
    event = VerifiedReading.model_validate(event)
    if event.reading is None:
        return ProcessedReading(provenance=event, quality_flags=('malformed_event',))
    raw = event.reading
    # Explicit dataset quantities preserve source units; legacy temperature stays C.
    grid_units = {'T_zAvgTerr_HM':'K', 'T_zAvgTerr_HMin':'K',
                  'T_zAvgTerr_HMax':'K', 'RH_HM':'%', 'Precip_HS':'mm'}
    grid_units.update({k.replace('_HM','_HHM').replace('_HS','_HHS'):v for k,v in list(grid_units.items())})
    grid_units['T_z300m_HHM'] = 'K'
    if raw.quantity in grid_units:
        expected = grid_units[raw.quantity]
        flags = []
        if raw.unit != expected:
            flags.append('unknown_unit')
        if raw.value is None:
            flags.append('missing_value')
        elif expected == 'K' and raw.value < 0:
            flags.append('below_absolute_zero')
        result = TemperatureResult(raw.value if not flags else None, tuple(flags))
        normalized_unit = expected
    else:
        result = normalize_temperature(raw.value, raw.unit, raw.quantity)
        normalized_unit = 'C'
    flags = list(result.flags)
    if event.status != 'valid':
        flags.insert(0, 'provenance_ineligible')
    normalized = result.value_c if event.analysis_eligible else None
    return ProcessedReading(provenance=event, normalized_value=normalized,
                            normalized_unit=normalized_unit if normalized is not None else None,
                            quality_flags=tuple(flags))
