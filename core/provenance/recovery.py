"""Explicit bounded retention of exact signed bytes for operator-directed recovery."""
from contextlib import closing, contextmanager
from pathlib import Path
import os
import sqlite3
import stat

from core.provenance.codec import decode_reading, encode_reading
from core.secure_files import no_symlinks, write_new_private


class RecoveryArchive:
    def __init__(self, path: Path):
        self.path = no_symlinks(path)
        self._check()
        with self.transaction() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('unsupported archive version')

    def _check(self):
        no_symlinks(self.path)
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('archive must be a regular unlinked file')
        if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PermissionError('archive must be private and owned by this user')

    @classmethod
    def create(cls,path: Path, *, capacity=10000):
        if type(capacity) is not int or not 1 <= capacity <= 1000000:
            raise ValueError('invalid archive capacity')
        write_new_private(path,b'')
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''PRAGMA user_version=1;
                CREATE TABLE limits(capacity INTEGER NOT NULL);
                CREATE TABLE records(sensor TEXT,key_id TEXT,stream TEXT,sequence INTEGER,payload BLOB NOT NULL,
                    PRIMARY KEY(sensor,key_id,stream,sequence));''')
            db.execute('INSERT INTO limits VALUES (?)',(capacity,))
        return cls(path)

    @contextmanager
    def transaction(self):
        self._check()
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=10)
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

    def retain(self, reading):
        payload=encode_reading(reading)
        with self.transaction() as db:
            old=db.execute('SELECT payload FROM records WHERE sensor=? AND key_id=? AND stream=? AND sequence=?',reading.identity).fetchone()
            if old:
                if old[0]!=payload:
                    raise ValueError('conflicting signed bytes for retained identity')
                return
            count=db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
            capacity=db.execute('SELECT capacity FROM limits').fetchone()[0]
            if count>=capacity:
                raise BufferError('recovery archive full; no evidence deleted, sender record remains pending')
            db.execute('INSERT INTO records VALUES (?,?,?,?,?)',(*reading.identity,payload))

    def records(self, identity, start, stop):
        if len(identity)!=3 or any(type(x) is not str for x in identity):
            raise ValueError('sensor/key/stream identity required')
        if type(start) is not int or type(stop) is not int or not 0<=start<stop<=2**63-1 or stop-start>10000:
            raise ValueError('explicit half-open sequence range of at most 10000 required')
        with self.transaction() as db:
            rows=db.execute('SELECT sequence,payload FROM records WHERE sensor=? AND key_id=? AND stream=? AND sequence>=? AND sequence<? ORDER BY sequence',(*identity,start,stop)).fetchall()
        if [r[0] for r in rows]!=list(range(start,stop)):
            raise ValueError('requested recovery range is incomplete; nothing sent')
        result=tuple(decode_reading(row[1]) for row in rows)
        if any(r.identity!=(*identity,start+i) for i,r in enumerate(result)):
            raise ValueError('archive identity does not match payload')
        return result

    def status(self):
        with self.transaction() as db:
            return {'records':db.execute('SELECT COUNT(*) FROM records').fetchone()[0],
                    'capacity':db.execute('SELECT capacity FROM limits').fetchone()[0]}
