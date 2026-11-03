"""Explicit event-time policy; injected UTC clock supports deterministic tests."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math


@dataclass(frozen=True)
class ReplayPolicy:
    max_age_seconds: float = 120.0
    future_skew_seconds: float = 5.0

    def __post_init__(self):
        for value in (self.max_age_seconds, self.future_skew_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 86400:
                raise ValueError('time tolerances must be finite and between 0 and 86400 seconds')

    def finding(self, observed: datetime, now: datetime) -> str | None:
        if observed < now - timedelta(seconds=self.max_age_seconds):
            return 'timestamp_too_old'
        if observed > now + timedelta(seconds=self.future_skew_seconds):
            return 'timestamp_in_future'
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('verifier clock must return an aware datetime')
    return value.astimezone(timezone.utc)
