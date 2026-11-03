"""Locally administered trust anchors and durable receiver state; never TOFU."""
from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import sqlite3
import os
import stat

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import TypeAdapter

from core.contracts import Identifier
from core.local_config import contained_file
from core.provenance.codec import GENESIS_HASH
from core.secure_files import no_symlinks, read_private, write_new_private

identifier = TypeAdapter(Identifier)


def public_key_id(key: Ed25519PublicKey) -> str:
    return 'ed25519-' + hashlib.sha256(key.public_bytes_raw()).hexdigest()[:32]


class KeyRegistry:
    """One SQLite database serializes enrollment, revocation, verification and audit.

    Administration is a local trusted operation. No remote envelope can enroll
    a key or stream. Retired keys and replay heads are never deleted.
    """
    def __init__(self, path: Path):
        self.path = no_symlinks(path)
        # State may grow, so validate ownership separately from bounded key reads.
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("receiver state must be a regular unlinked file")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError("receiver state must be private and owned by this user")
        with self.transaction() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('unsupported receiver database version')
            # Keep pending-count/routing bounded as the retained audit journal grows.
            db.execute('CREATE INDEX IF NOT EXISTS events_pending ON events(id) WHERE delivered=0')

    @classmethod
    def create(cls, path: Path) -> 'KeyRegistry':
        write_new_private(path, b'')
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                PRAGMA user_version=1;
                CREATE TABLE keys(sensor TEXT, key_id TEXT, public BLOB NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(sensor,key_id));
                CREATE TABLE heads(sensor TEXT, key_id TEXT, stream TEXT,
                    next_sequence INTEGER NOT NULL DEFAULT 0, previous BLOB NOT NULL,
                    timestamp TEXT, PRIMARY KEY(sensor,key_id,stream));
                CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    verdict TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
            ''')
        return cls(path)

    @contextmanager
    def transaction(self):
        no_symlinks(self.path)
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enroll(self, sensor: str, key: Ed25519PublicKey, stream: str,
               *, retire_key: str | None = None) -> str:
        identifier.validate_python(sensor)
        identifier.validate_python(stream)
        if not isinstance(key, Ed25519PublicKey):
            raise TypeError('Ed25519 public key required')
        kid = public_key_id(key)
        with self.transaction() as db:
            existing = db.execute('SELECT revoked FROM keys WHERE sensor=? AND key_id=?',
                                  (sensor, kid)).fetchone()
            if existing and existing[0]:
                raise ValueError('revoked keys cannot be reactivated')
            if retire_key is not None:
                if retire_key == kid or not db.execute(
                    'SELECT 1 FROM keys WHERE sensor=? AND key_id=?', (sensor, retire_key)).fetchone():
                    raise ValueError('rotation requires a distinct enrolled predecessor')
            db.execute('INSERT OR IGNORE INTO keys VALUES (?,?,?,0)',
                       (sensor, kid, key.public_bytes_raw()))
            # Duplicate enrollment fails, never resets a live stream.
            db.execute('INSERT INTO heads VALUES (?,?,?,0,?,NULL)',
                       (sensor, kid, stream, GENESIS_HASH))
            if retire_key is not None:
                db.execute('UPDATE keys SET revoked=1 WHERE sensor=? AND key_id=?', (sensor, retire_key))
        return kid

    def revoke(self, sensor: str, key_id: str) -> None:
        with self.transaction() as db:
            if db.execute('UPDATE keys SET revoked=1 WHERE sensor=? AND key_id=?',
                          (sensor, key_id)).rowcount != 1:
                raise ValueError('key is not enrolled')

    @classmethod
    def import_bundle(cls, path: Path, bundle: Path) -> 'KeyRegistry':
        data = json.loads(read_private(bundle / 'public_registry.json', max_bytes=65536))
        if data.get('schema_version') != '1.0' or not 1 <= len(data['sensors']) <= 32:
            raise ValueError('invalid public registry bundle')
        validated = []
        identities = set()
        for item in data['sensors']:
            sensor, stream = item['sensor_id'], item['stream_id']
            identifier.validate_python(sensor)
            identifier.validate_python(stream)
            key = serialization.load_pem_public_key(read_private(contained_file(bundle, item['public_key'])))
            if not isinstance(key, Ed25519PublicKey) or public_key_id(key) != item['key_id']:
                raise ValueError('public key fingerprint mismatch')
            identity = (sensor, item['key_id'], stream)
            if identity in identities:
                raise ValueError('duplicate enrollment')
            identities.add(identity)
            validated.append((sensor, key, stream))
        registry = cls.create(path)
        for sensor, key, stream in validated:
            registry.enroll(sensor, key, stream)
        return registry
