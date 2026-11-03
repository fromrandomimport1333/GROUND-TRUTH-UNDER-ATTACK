"""Layer 4 audit log: append-only SQLite table with its own SHA-256 hash chain.

Every entry stores ``hash = SHA256(prev_hash || canonical JSON of the entry)``; the first entry
chains from 64 zeros. SQLite triggers reject UPDATE and DELETE, so rewriting history through
this database requires dropping the triggers, and any edit made outside SQLite (or after dropping
them) is detected by :meth:`AuditLog.verify`, which recomputes the whole chain.

``dedup_key`` (unique, optional) makes at-least-once inputs idempotent: Layer 1's
``ProvenancePipeline`` may redeliver a verdict, and the second append is ignored.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    sensor_id  TEXT,
    dedup_key  TEXT UNIQUE,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def canonical(value) -> str:
    """Deterministic JSON (sorted keys, no whitespace, no NaN) used for hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def entry_hash(prev_hash, seq, ts, kind, sensor_id, dedup_key, payload_json) -> str:
    body = canonical({"seq": seq, "ts": ts, "kind": kind, "sensor_id": sensor_id,
                      "dedup_key": dedup_key, "payload": payload_json})
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


class AuditLog:
    """Append-only, hash-chained audit trail.

    Several processes may append (the Layer 4 engine and the dashboard's analyst releases): each append
    reads the chain head and inserts inside one ``BEGIN IMMEDIATE`` transaction, so the chain stays linear.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(_SCHEMA)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def head(self):
        row = self.db.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def append(self, ts, kind, payload, sensor_id=None, dedup_key=None):
        """Append one entry; returns its sequence number, or ``None`` if ``dedup_key`` was already logged."""
        payload_json = canonical(payload)
        ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if dedup_key is not None and self.db.execute(
                    "SELECT 1 FROM audit_log WHERE dedup_key = ?", (dedup_key,)).fetchone():
                self.db.execute("ROLLBACK")
                return None
            seq, prev = self.head()
            seq += 1
            digest = entry_hash(prev, seq, ts, kind, sensor_id, dedup_key, payload_json)
            self.db.execute("INSERT INTO audit_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (seq, ts, kind, sensor_id, dedup_key, payload_json, prev, digest))
            self.db.execute("COMMIT")
            return seq
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def entries(self, kind=None, sensor_id=None, after=0):
        """Yield entries as dicts (payload decoded), in sequence order, with ``seq > after``."""
        query, args = "SELECT seq, ts, kind, sensor_id, dedup_key, payload, prev_hash, hash FROM audit_log", []
        clauses = ["seq > ?"]
        args.append(after)
        for column, value in (("kind", kind), ("sensor_id", sensor_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                args.append(value)
        query += " WHERE " + " AND ".join(clauses)
        for row in self.db.execute(query + " ORDER BY seq", args):
            yield dict(zip(("seq", "ts", "kind", "sensor_id", "dedup_key", "payload", "prev_hash", "hash"),
                           (*row[:5], json.loads(row[5]), *row[6:])))

    def verify(self):
        """Recompute the chain. Returns ``(ok, count, first_bad_seq)``."""
        prev, expected_seq, count = GENESIS, 1, 0
        with closing(self.db.cursor()) as cur:
            for seq, ts, kind, sensor, dedup, payload, prev_hash, digest in cur.execute(
                    "SELECT seq, ts, kind, sensor_id, dedup_key, payload, prev_hash, hash FROM audit_log ORDER BY seq"):
                if seq != expected_seq or prev_hash != prev or \
                        entry_hash(prev, seq, ts, kind, sensor, dedup, payload) != digest:
                    return False, count, seq
                prev, expected_seq, count = digest, seq + 1, count + 1
        return True, count, None
