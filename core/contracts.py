"""Provisional v1 internal contracts, independent of any dataset format.

These models validate structure, not cryptographic truth. Only the runtime
verifier may create trusted VerifiedReading objects at the runtime boundary.
Never deserialize a remote VerifiedReading as evidence of verification.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer,
    StrictBool, StrictFloat, StrictInt, StrictStr, WithJsonSchema, field_validator, model_validator,
)


Identifier = Annotated[StrictStr, Field(min_length=1, max_length=64,
                                      pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Unit = Annotated[StrictStr, Field(min_length=1, max_length=32,
                                pattern=r"^[^\x00-\x1f\x7f]+$")]
Sequence = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
def _float_only(value: object) -> float:
    if type(value) is not float:
        raise ValueError("a float is required; convert dataset numbers explicitly in an adapter")
    return value


FiniteNumber = Annotated[StrictFloat, BeforeValidator(_float_only), Field(allow_inf_nan=False)]
ProvenanceStatus = Literal[
    "valid", "bad_signature", "unknown_sensor", "replay", "gap",
    "out_of_window", "chain_anomaly", "revoked_key", "malformed",
]
Availability = Literal["not_computed", "available", "unavailable"]


def _utc(value: object) -> datetime:
    if isinstance(value, str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("expected an ISO 8601 timestamp with an explicit timezone")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, BeforeValidator(_utc)]


def _binary(value: object, size: int) -> bytes:
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9a-f]{" + str(size * 2) + "}", value):
            raise ValueError(f"expected {size * 2} lowercase hexadecimal characters")
        value = bytes.fromhex(value)
    if not isinstance(value, bytes) or len(value) != size:
        raise ValueError(f"expected exactly {size} bytes")
    return value


Hash32 = Annotated[
    bytes, BeforeValidator(lambda v: _binary(v, 32)),
    PlainSerializer(lambda v: v.hex(), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": "^[0-9a-f]{64}$"}),
]
Signature64 = Annotated[
    bytes, BeforeValidator(lambda v: _binary(v, 64)),
    PlainSerializer(lambda v: v.hex(), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": "^[0-9a-f]{128}$"}),
]


class Contract(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, validate_default=True,
        revalidate_instances="always", allow_inf_nan=False,
    )


class Location(Contract):
    latitude: Annotated[FiniteNumber, Field(ge=-90, le=90)]
    longitude: Annotated[FiniteNumber, Field(ge=-180, le=180)]


class SignedReading(Contract):
    """A structurally valid envelope, which may still be forged or untruthful.

    Unit strings are intentionally not limited to C/F/K. Phase 4 applies
    quantity-specific unit semantics without discarding authenticated evidence.
    A null value represents an explicitly signed missing observation.
    """

    schema_version: Literal["1.0"]
    sensor_id: Identifier
    key_id: Identifier
    stream_id: Identifier
    timestamp: Annotated[StrictStr, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")]
    sequence: Sequence
    quantity: Identifier
    value: FiniteNumber | None
    unit: Unit
    location: Location | None = None
    prev_hash: Hash32
    signature: Signature64

    @field_validator("timestamp")
    @classmethod
    def canonical_timestamp(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value):
            raise ValueError("signed timestamp must be UTC YYYY-MM-DDTHH:MM:SS.ffffffZ")
        _utc(value)  # Check the calendar, retaining the exact signed string.
        return value

    @property
    def observed_at(self) -> datetime:
        return _utc(self.timestamp)

    @property
    def identity(self) -> tuple[str, str, str, int]:
        return self.sensor_id, self.key_id, self.stream_id, self.sequence


class VerifiedReading(Contract):
    """Internal verifier output. Construction does not perform verification."""

    schema_version: Literal["1.0"]
    reading: SignedReading | None
    status: ProvenanceStatus
    signature_verified: StrictBool | None
    checked_at: UtcDatetime
    findings: tuple[Identifier, ...] = Field(default=(), max_length=32)
    raw_message_sha256: Hash32 | None = None

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        if self.status == "malformed":
            if self.reading is not None or self.signature_verified is not None:
                raise ValueError("malformed input has no reading or signature verdict")
            if self.raw_message_sha256 is None:
                raise ValueError("malformed input requires a bounded raw-message digest")
        elif self.reading is None:
            raise ValueError("this status requires the original signed envelope")
        expected = {
            "valid": True, "replay": True, "gap": True,
            "out_of_window": True, "chain_anomaly": True,
            "bad_signature": False, "unknown_sensor": None, "revoked_key": None,
        }
        if self.status in expected and self.signature_verified is not expected[self.status]:
            raise ValueError("signature verdict is inconsistent with provenance status")
        return self

    @property
    def analysis_eligible(self) -> bool:
        # Only contiguous authenticated observations are eligible; gaps stay quarantined.
        return self.status == "valid" and self.reading is not None and self.reading.value is not None


class ProcessedReading(Contract):
    """A derived observation retaining its full raw envelope and verdict."""

    provenance: VerifiedReading
    normalized_value: FiniteNumber | None = None
    normalized_unit: Unit | None = None
    quality_flags: tuple[Identifier, ...] = Field(default=(), max_length=32)

    @property
    def ready_for_analysis(self) -> bool:
        return self.provenance.analysis_eligible and self.normalized_value is not None

    @model_validator(mode="after")
    def consistent_normalization(self) -> Self:
        if (self.normalized_value is None) != (self.normalized_unit is None):
            raise ValueError("normalized value and unit must both be present or both absent")
        if self.normalized_value is not None and not self.provenance.analysis_eligible:
            raise ValueError("ineligible observations cannot carry normalized analytical values")
        return self


class FeatureValue(Contract):
    name: Identifier
    sensor_id: Identifier | None = None
    value: FiniteNumber | None
    unit: Unit | None = None


class FeatureGroup(Contract):
    availability: Availability = "not_computed"
    values: tuple[FeatureValue, ...] = Field(default=(), max_length=4096)
    reason: Identifier | None = None

    @model_validator(mode="after")
    def consistent_availability(self) -> Self:
        if self.availability != "available" and self.values:
            raise ValueError("unavailable or uncomputed groups cannot carry features")
        if self.availability == "available" and not self.values:
            raise ValueError("an available group must contain features")
        if self.availability == "unavailable" and self.reason is None:
            raise ValueError("unavailable features require a reason")
        keys = [(v.sensor_id, v.name) for v in self.values]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate feature identifiers")
        return self


class ReferenceObservation(Contract):
    source_id: Identifier
    observed_at: UtcDatetime
    available_at: UtcDatetime
    quantity: Identifier
    value: FiniteNumber | None
    unit: Unit
    location: Location | None = None


class FeatureWindow(Contract):
    """Provisional L2 -> L3 interface, including evidence for L4 routing.

    The interval is [start, end); as_of is the information availability cutoff.
    No geographic neighbors or reference observations are invented when absent.
    """

    schema_version: Literal["1.0"]
    window_id: Identifier
    start: UtcDatetime
    end: UtcDatetime
    as_of: UtcDatetime
    readings: tuple[ProcessedReading, ...] = Field(default=(), max_length=10000)
    temporal_features: FeatureGroup = Field(default_factory=FeatureGroup)
    spatial_features: FeatureGroup = Field(default_factory=FeatureGroup)
    reference_features: FeatureGroup = Field(default_factory=FeatureGroup)
    reference_data: tuple[ReferenceObservation, ...] = Field(default=(), max_length=10000)

    @model_validator(mode="after")
    def consistent_window(self) -> Self:
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        if self.as_of < self.end:
            raise ValueError("availability cutoff must be at or after window end")
        identities = []
        for item in self.readings:
            raw = item.provenance.reading
            if raw is None:
                raise ValueError("malformed events are routed to L4 outside observation windows")
            if not self.start <= raw.observed_at < self.end:
                raise ValueError("reading falls outside the half-open window")
            if item.provenance.checked_at > self.as_of:
                raise ValueError("reading was verified after the availability cutoff")
            identities.append(raw.identity)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate reading identity in window")
        for reference in self.reference_data:
            if reference.observed_at >= self.end or reference.available_at > self.as_of:
                raise ValueError("reference exceeds the event-time or availability cutoff")
        return self


# ---------------------------------------------------------------- Layer 4 (trust and response)

TrustState = Literal["trusted", "suspicious", "quarantined"]
Probability = Annotated[FiniteNumber, Field(ge=0, le=1)]


class VariableEvidence(Contract):
    """One variable of one sensor at one tick: the L1 verdict, the L3 score, and what L4 released.

    ``raw_value`` is always kept (source units). ``release`` says what downstream consumers get:
    the raw value, a neighbour-median substitute, or nothing.
    """

    variable: Identifier
    quantity: Identifier
    provenance_status: Identifier          # an L1 status, "missing" (no reading) or "ambiguous"
    event_id: Annotated[StrictInt, Field(ge=0)] | None = None   # L1 durable event ID of the reading
    raw_value: FiniteNumber | None = None
    unit: Unit | None = None
    p_value: Probability | None = None     # L3 calibrated tail value; None = not scored
    alpha: Probability | None = None       # frozen L3 alert threshold for this variable
    alert: StrictBool = False              # L3 alert (after its confirmation rule)
    anomaly: Probability | None = None     # L4 mapping of (p_value, alert) to [0, 1]
    released_value: FiniteNumber | None = None
    release: Literal["raw", "imputed", "withheld"]
    imputation_neighbors: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def consistent_release(self) -> Self:
        if (self.release == "withheld") != (self.released_value is None):
            raise ValueError("withheld values carry no released value, and vice versa")
        if self.release == "raw" and self.released_value != self.raw_value:
            raise ValueError("a raw release must equal the raw value")
        return self


class TrustDecision(Contract):
    """L4 output per sensor per tick.

    ``category`` separates the two cases the architecture requires reporting separately:
    ``provenance_failure`` (L1 rejected a reading: overrides everything) and
    ``authentic_but_suspicious`` (valid signature, L3 alert: physical spoofing / compromised node).
    """

    schema_version: Literal["1.0"] = "1.0"
    sensor_id: Identifier
    ts: UtcDatetime
    window_id: Identifier | None = None
    trust_score: Probability
    state: TrustState
    previous_state: TrustState
    category: Literal["nominal", "unscored", "authentic_but_suspicious", "provenance_failure"]
    cause: Literal["provenance", "anomaly"] | None = None   # why the sensor is not trusted
    actions: tuple[Literal["flag", "quarantine", "impute", "notify"], ...] = ()
    alert_count_short: Annotated[StrictInt, Field(ge=0)] = 0     # alerted ticks in the short window
    alert_count_long: Annotated[StrictInt, Field(ge=0)] = 0      # alerted ticks in the long window
    provenance_failures_recent: Annotated[StrictInt, Field(ge=0)] = 0
    explanation: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    evidence: tuple[VariableEvidence, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def consistent_decision(self) -> Self:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("duplicate actions")
        if self.state == "trusted" and self.cause is not None:
            raise ValueError("a trusted sensor has no distrust cause")
        if self.state != "trusted" and self.cause is None:
            raise ValueError("a non-trusted state requires a cause")
        return self
