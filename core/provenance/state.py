"""Atomic sequence/hash-chain allocation plus a durable bounded MQTT outbox."""

from __future__ import annotations

from pathlib import Path
from contextlib import closing
import sqlite3
from typing import Callable

from core.contracts import SignedReading
from core.provenance.codec import GENESIS_HASH, decode_reading, encode_reading, record_hash
from core.secure_files import no_symlinks, write_new_private


class SignerState:
    def __init__(self, path: Path):
        self.path = no_symlinks(path)
        if not self.path.is_file():
            raise FileNotFoundError("provisioned signer database missing; refusing a silent chain reset")

    @classmethod
    def create(cls, path: Path, streams: list[tuple[str, str, str]]) -> "SignerState":
        if not streams or len(set(streams)) != len(streams):
            raise ValueError("unique provisioned streams required")
        write_new_private(path, b"")
        state = cls(path)
        with closing(state._connection()) as db, db:
            db.executescript("""
                PRAGMA user_version=1;
                CREATE TABLE streams (
                    sensor TEXT NOT NULL, key_id TEXT NOT NULL, stream TEXT NOT NULL,
                    next_sequence INTEGER NOT NULL CHECK(next_sequence >= 0),
                    prev_hash BLOB NOT NULL CHECK(length(prev_hash)=32),
                    last_timestamp TEXT,
                    PRIMARY KEY(sensor,key_id,stream));
                CREATE TABLE outbox (
                    sensor TEXT NOT NULL, key_id TEXT NOT NULL, stream TEXT NOT NULL,
                    sequence INTEGER NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(sensor,key_id,stream,sequence));
            """)
            db.executemany("INSERT INTO streams VALUES (?,?,?,0,?,NULL)",
                           [(*stream, GENESIS_HASH) for stream in streams])
        return state

    def _connection(self) -> sqlite3.Connection:
        no_symlinks(self.path)
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def issue(self, identity: tuple[str, str, str],
              factory: Callable[[int, bytes], SignedReading], *, max_pending: int = 128) -> SignedReading:
        if not 1 <= max_pending <= 10000:
            raise ValueError("invalid pending-message bound")
        db = self._connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("unknown signer state version")
            row = db.execute("SELECT next_sequence,prev_hash,last_timestamp FROM streams "
                             "WHERE sensor=? AND key_id=? AND stream=?", identity).fetchone()
            if row is None:
                raise ValueError("unprovisioned key/stream; explicit enrollment required")
            pending = db.execute("SELECT COUNT(*) FROM outbox WHERE sensor=? AND key_id=? AND stream=?",
                                 identity).fetchone()[0]
            if pending >= max_pending:
                raise BufferError("outbox full; refusing to discard signed evidence")
            sequence, previous, last_timestamp = row
            if sequence >= 2**63 - 1:
                raise OverflowError("sequence space exhausted; explicit stream rotation required")
            reading = SignedReading.model_validate(factory(sequence, previous))
            if reading.identity != (*identity, sequence) or reading.prev_hash != previous:
                raise ValueError("factory changed provisioned identity or chain state")
            if last_timestamp is not None and reading.timestamp < last_timestamp:
                raise ValueError("clock moved backwards; refusing to advance signer state")
            payload = encode_reading(reading)
            db.execute("INSERT INTO outbox VALUES (?,?,?,?,?)", (*identity, sequence, payload))
            db.execute("UPDATE streams SET next_sequence=?,prev_hash=?,last_timestamp=? "
                       "WHERE sensor=? AND key_id=? AND stream=?",
                       (sequence + 1, record_hash(reading), reading.timestamp, *identity))
            db.commit()
            return reading
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pending(self, identity: tuple[str, str, str]) -> tuple[SignedReading, ...]:
        with closing(self._connection()) as db, db:
            rows = db.execute("SELECT payload FROM outbox WHERE sensor=? AND key_id=? AND stream=? "
                              "ORDER BY sequence", identity).fetchall()
        return tuple(decode_reading(row[0]) for row in rows)

    def acknowledge(self, reading: SignedReading) -> None:
        reading = SignedReading.model_validate(reading)
        with closing(self._connection()) as db, db:
            db.execute("DELETE FROM outbox WHERE sensor=? AND key_id=? AND stream=? AND sequence=? "
                       "AND payload=?", (*reading.identity, encode_reading(reading)))
