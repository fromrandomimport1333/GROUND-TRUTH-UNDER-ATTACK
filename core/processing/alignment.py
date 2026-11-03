"""Fixed UTC half-open windows, using exact integer microseconds."""
from datetime import datetime, timedelta, timezone
from core.provenance.replay_protection import require_utc

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def window_bounds(timestamp: datetime, seconds: int = 60) -> tuple[datetime, datetime]:
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError('window duration must be 1..86400 integer seconds')
    stamp = require_utc(timestamp)
    delta = stamp - EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    width = seconds * 1000000
    start = EPOCH + timedelta(microseconds=(micros // width) * width)
    return start, start + timedelta(seconds=seconds)
