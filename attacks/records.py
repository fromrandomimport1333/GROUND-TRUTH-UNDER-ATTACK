"""Record-level (category B/C) faults on a stream of signed sensor records.

These faults attack the transport/storage of records, not the physical value, so the layer-1
verifier (signatures, sequence numbers, hash chain, sensor registry) must catch them.

Record format (the teammates' L1 message): a dict with the keys ``sensor_id, seq, timestamp,
value, unit, prev_hash, signature``. Conventions this module relies on:

* ``seq`` is counted per ``sensor_id`` (first record 0, then +1 per record).
* ``prev_hash`` is the hash chain over one sensor's records: ``GENESIS_HASH`` for the first
  record, else ``record_hash`` (sha256 over the canonical JSON, signature included) of the
  previous record of the *same* sensor.
* The signature covers every field except itself, so any change made without the key breaks it.
* The stream is the arrival order at the verifier (time-major when several sensors interleave).

Every transform returns ``(records_out, labels_rows)``, never mutates its input and shares no
record objects with it. A label row is ``{idx, fault, expected_status, stage}`` where ``idx`` is the
position of the record in ``records_out`` and ``expected_status`` is what a correct verifier must
report for it (one of bad_signature, replay, gap, unknown_sensor).

Labels mark the faulted record only. A record rejected in place (tamper, shifted timestamp,
rewrite, unknown sensor) leaves a hole in its sensor's sequence, so a verifier that advances state
only on accepted records will also report ``gap`` on that sensor's next record. That follow-on is a
consequence of the fault, not a false positive; consumers scoring false positives should allow it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from typing import Callable

RECORD_KEYS = ("sensor_id", "seq", "timestamp", "value", "unit", "prev_hash", "signature")
GENESIS_HASH = "0" * 64

Record = dict
Label = dict
Signer = Callable[[dict, str], dict]


# ------------------------------------------------------------------ stream building


def record_hash(record: Record) -> str:
    """sha256 hex of the canonical JSON of a full record (signature included)."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def build_signed_stream(readings: list[dict], sign: Signer) -> list[Record]:
    """Turn readings (``sensor_id, timestamp, value, unit``, in arrival order) into signed records.

    Assigns per-sensor ``seq`` and the per-sensor hash chain, and calls
    ``sign(record_without_signature, prev_hash)`` for each record. The signer is injected, so
    this module never depends on a concrete (and especially not on a test) signer.
    """
    last: dict[str, Record] = {}
    stream: list[Record] = []
    for reading in readings:
        sid = reading["sensor_id"]
        prev = last.get(sid)
        prev_hash = GENESIS_HASH if prev is None else record_hash(prev)
        unsigned = {
            "sensor_id": sid,
            "seq": 0 if prev is None else prev["seq"] + 1,
            "timestamp": reading["timestamp"],
            "value": reading["value"],
            "unit": reading["unit"],
            "prev_hash": prev_hash,
        }
        record = sign(unsigned, prev_hash)
        if set(record) != set(RECORD_KEYS):
            raise ValueError(f"signer returned keys {sorted(record)}, expected {sorted(RECORD_KEYS)}")
        record = copy.deepcopy(record)
        last[sid] = record
        stream.append(record)
    return stream


# ------------------------------------------------------------------------ helpers


def _label(idx: int, fault: str, status: str, stage: str = "transit") -> Label:
    return {"idx": idx, "fault": fault, "expected_status": status, "stage": stage}


def _check_index(records: list[Record], idx: int) -> None:
    if not 0 <= idx < len(records):
        raise IndexError(f"record index {idx} out of range for a stream of {len(records)}")


def _nonzero(name: str, delta: float) -> None:
    if delta == 0:
        raise ValueError(f"{name} must be non-zero, otherwise the record is unchanged but labelled")


def _shift_time(timestamp, delta_s: float):
    if isinstance(timestamp, (int, float)):
        return timestamp + delta_s
    shifted = (datetime.fromisoformat(timestamp) + timedelta(seconds=delta_s)).isoformat()
    return shifted.replace("+00:00", "Z") if timestamp.endswith("Z") else shifted


def _edit_value(records: list[Record], idx: int, delta: float, fault: str, stage: str):
    _check_index(records, idx)
    _nonzero("delta", delta)
    out = copy.deepcopy(records)
    out[idx]["value"] = out[idx]["value"] + delta  # signature deliberately left stale
    return out, [_label(idx, fault, "bad_signature", stage)]


# ------------------------------------------------------------------- transforms


def tamper_value(records: list[Record], idx: int, delta: float):
    """Change a value in transit without re-signing -> ``bad_signature``."""
    return _edit_value(records, idx, delta, "tamper_value", "transit")


def rewrite_stored(records: list[Record], idx: int, delta: float):
    """Change a value in the stored log without re-signing -> ``bad_signature``, stage ``storage``."""
    return _edit_value(records, idx, delta, "rewrite_stored", "storage")


def shift_timestamp(records: list[Record], idx: int, delta_s: float):
    """Move a record's timestamp by ``delta_s`` seconds without re-signing -> ``bad_signature``."""
    _check_index(records, idx)
    _nonzero("delta_s", delta_s)
    out = copy.deepcopy(records)
    out[idx]["timestamp"] = _shift_time(out[idx]["timestamp"], delta_s)
    return out, [_label(idx, "shift_timestamp", "bad_signature")]


def unknown_sensor(records: list[Record], idx: int, fake_id: str):
    """Present a record under a sensor id missing from the registry -> ``unknown_sensor``."""
    _check_index(records, idx)
    if not fake_id or any(r["sensor_id"] == fake_id for r in records):
        raise ValueError(f"fake_id {fake_id!r} must be non-empty and absent from the stream")
    out = copy.deepcopy(records)
    out[idx]["sensor_id"] = fake_id
    return out, [_label(idx, "unknown_sensor", "unknown_sensor")]


def replay(records: list[Record], src_idx: int, at_idx: int):
    """Re-send an earlier, validly signed record so that it lands at position ``at_idx``.

    The copy is inserted (output is one record longer). Its sequence number was already accepted,
    so the verifier must report ``replay`` for it; the genuine records are unaffected.
    """
    _check_index(records, src_idx)
    if not 0 <= at_idx <= len(records):
        raise IndexError(f"insert position {at_idx} out of range for a stream of {len(records)}")
    if at_idx <= src_idx:
        raise ValueError("a replay must land after the record it copies (at_idx > src_idx)")
    out = copy.deepcopy(records)
    out.insert(at_idx, copy.deepcopy(records[src_idx]))
    return out, [_label(at_idx, "replay", "replay")]


def drop(records: list[Record], idxs: list[int]):
    """Remove records -> ``gap`` on the next kept record of each affected sensor.

    The gap is only observable when a later record of the same sensor arrives, so each dropped
    record maps to the first kept record of its sensor after it (consecutive drops of one sensor
    share one label). Records dropped from the tail of a sensor's stream produce no label.
    """
    dropped = set(idxs)
    if not dropped:
        raise ValueError("drop needs at least one index")
    for k in dropped:
        _check_index(records, k)
    flagged = set()
    for k in sorted(dropped):
        sid = records[k]["sensor_id"]
        for m in range(k + 1, len(records)):
            if m not in dropped and records[m]["sensor_id"] == sid:
                flagged.add(m)
                break
    new_idx, out = {}, []
    for k, rec in enumerate(records):
        if k not in dropped:
            new_idx[k] = len(out)
            out.append(copy.deepcopy(rec))
    return out, [_label(new_idx[m], "drop", "gap") for m in sorted(flagged)]


def reorder(records: list[Record], i: int, j: int):
    """Swap two consecutive records of one sensor.

    The newer record now arrives first: its sequence number jumps ahead (``gap``). The older one
    arrives afterwards with a sequence number already passed (``replay``). Both signatures stay
    valid. Swapping records of different sensors, or non-adjacent records of one sensor (whose
    outcome depends on how the verifier resynchronises), is rejected.
    """
    _check_index(records, i)
    _check_index(records, j)
    lo, hi = sorted((i, j))
    sid = records[lo]["sensor_id"]
    if lo == hi or records[hi]["sensor_id"] != sid:
        raise ValueError("reorder needs two different records of the same sensor")
    if any(records[k]["sensor_id"] == sid for k in range(lo + 1, hi)):
        raise ValueError("reorder needs records that are consecutive for their sensor")
    out = copy.deepcopy(records)
    out[lo], out[hi] = out[hi], out[lo]
    return out, [_label(lo, "reorder", "gap"), _label(hi, "reorder", "replay")]


FAULTS: dict[str, Callable] = {
    "tamper_value": tamper_value,
    "replay": replay,
    "drop": drop,
    "reorder": reorder,
    "shift_timestamp": shift_timestamp,
    "rewrite_stored": rewrite_stored,
    "unknown_sensor": unknown_sensor,
}


def apply_faults(records: list[Record], plan: list[tuple[str, dict]]):
    """Apply several faults to one stream and keep every label pointing at the right record.

    ``plan`` is ``[(fault_name, kwargs), ...]`` with kwargs for that transform, ordered by strictly
    descending stream position and spaced apart. Working from the end means an insert (replay) or
    removal (drop) only shifts the labels already produced, never the positions still to come.
    A plan that violates this raises ``ValueError``.
    """
    out, labels = copy.deepcopy(records), []
    for name, kwargs in plan:
        before = out
        out, new = FAULTS[name](before, **kwargs)
        shift = len(out) - len(before)
        moved = [{**old, "idx": old["idx"] + shift} for old in labels]
        for old, now in zip(labels, moved):
            if not 0 <= now["idx"] < len(out) or out[now["idx"]] != before[old["idx"]]:
                raise ValueError("plan must list faults in descending stream position, spaced apart")
        labels = new + moved
    return out, sorted(labels, key=lambda row: row["idx"])
