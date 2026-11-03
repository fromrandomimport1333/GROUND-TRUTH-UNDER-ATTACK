"""Bounded volatile state for replayable OFFLINE dataset exports only.

The production signer and verifier still execute all checks. Only their SQLite
storage is volatile: a crash requires replay from the original dataset. Never
use these stores with live sensor credentials or for MQTT delivery.
"""
from contextlib import contextmanager, closing
import sqlite3

from core.provenance.keyregistry import KeyRegistry
from core.provenance.state import SignerState


class _OwnedConnection(sqlite3.Connection):
    def close(self):
        # Production SignerState closes each operation's connection. This handle
        # belongs to the offline session, which explicitly disposes it at exit.
        pass


def _copy_to_memory(path):
    memory = sqlite3.connect(':memory:', factory=_OwnedConnection)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
            source.backup(memory)
    except BaseException:
        sqlite3.Connection.close(memory)
        raise
    return memory


class OfflineSignerState(SignerState):
    def __init__(self, existing):
        self.path = existing.path
        self.db = _copy_to_memory(self.path)

    def _connection(self):
        return self.db

    def close(self):
        sqlite3.Connection.close(self.db)


class OfflineKeyRegistry(KeyRegistry):
    def __init__(self, existing):
        self.path = existing.path
        self.db = _copy_to_memory(self.path)

    @contextmanager
    def transaction(self):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def discard_delivered(self):
        # Exporter has already serialized these events to its audit journal.
        # AUTOINCREMENT's sqlite_sequence survives DELETE, preserving event IDs.
        with self.transaction() as db:
            db.execute('DELETE FROM events WHERE delivered=1')

    def close(self):
        sqlite3.Connection.close(self.db)
