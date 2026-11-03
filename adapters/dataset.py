"""Dataset-independent input boundary. Concrete source schemas remain pending."""
from collections.abc import Iterable
from typing import Protocol

from pydantic import Field
from core.contracts import Contract, FiniteNumber, Identifier, Location, Unit, UtcDatetime


class Measurement(Contract):
    sensor_id: Identifier
    observed_at: UtcDatetime
    quantity: Identifier = 'temperature'
    value: FiniteNumber | None
    unit: Unit = 'C'
    location: Location | None = None
    source_id: Identifier
    source_record_id: str = Field(min_length=1, max_length=256)


class DatasetAdapter(Protocol):
    """Retain original files separately; source_record_id links back to exact rows.

    An adapter must explicitly map units, timezone, sensor IDs and missing values.
    Importing historical data does not establish original device authenticity.
    """
    def measurements(self) -> Iterable[Measurement]: ...
