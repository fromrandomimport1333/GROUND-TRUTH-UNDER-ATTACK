"""TEST-ONLY helpers for the record-level (category B/C) suite.

NEVER import this module from production code (``attacks/``, ``eval/``, ``data_prep/``).
It holds a throwaway HMAC signer with a hard-coded key and a toy reference verifier. The real
signer is the teammates' L1 signer, passed to ``attacks.build_records`` with ``--signer``.
``tests/test_records.py::test_helpers_not_imported_by_production_code`` enforces this.
"""

import hashlib
import hmac
import json

TEST_KEY = b"tsyp-test-only-hmac-key--do-not-use-in-production"
SIGNED_FIELDS = ("sensor_id", "seq", "timestamp", "value", "unit", "prev_hash")


def _message(record: dict) -> bytes:
    payload = {k: record[k] for k in SIGNED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign(record_without_signature: dict, prev_hash: str) -> dict:
    """HMAC-SHA256 over the canonical JSON of the six signed fields; returns a new full record."""
    record = {**record_without_signature, "prev_hash": prev_hash}
    record["signature"] = hmac.new(TEST_KEY, _message(record), hashlib.sha256).hexdigest()
    return record


def verify(record: dict) -> bool:
    expected = hmac.new(TEST_KEY, _message(record), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(record.get("signature", "")))


def reference_statuses(records: list[dict], known_sensors: set[str]) -> list[str]:
    """Toy L1 verifier used as an oracle for the labels. One status per record.

    Order of checks: registry -> signature -> per-sensor sequence. Per-sensor state advances only
    when a record is accepted (``ok`` or ``gap``), so a rejected record leaves its seq slot empty
    and the next record of that sensor shows up as ``gap``. Sequences start at 0.
    """
    last_seq: dict[str, int] = {}
    statuses = []
    for rec in records:
        sid = rec["sensor_id"]
        if sid not in known_sensors:
            statuses.append("unknown_sensor")
        elif not verify(rec):
            statuses.append("bad_signature")
        elif rec["seq"] <= last_seq.get(sid, -1):
            statuses.append("replay")
        else:
            gap = rec["seq"] > last_seq.get(sid, -1) + 1
            last_seq[sid] = rec["seq"]
            statuses.append("gap" if gap else "ok")
    return statuses
