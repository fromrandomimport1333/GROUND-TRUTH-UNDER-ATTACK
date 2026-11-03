"""L1b verification: authenticated sequence/chain changes and audit commit atomically."""
import hashlib
from typing import Callable
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.contracts import VerifiedReading
from core.provenance.codec import decode_reading, record_hash, signing_bytes
from core.provenance.keyregistry import KeyRegistry
from core.provenance.replay_protection import ReplayPolicy, require_utc, utc_now


class Verifier:
    def __init__(self, registry: KeyRegistry, *, policy: ReplayPolicy | None = None,
                 clock: Callable[[], datetime] = utc_now, max_pending: int = 10000):
        if type(max_pending) is not int or not 1 <= max_pending <= 1000000:
            raise ValueError('invalid pending audit bound')
        self.registry, self.policy, self.clock = registry, policy or ReplayPolicy(), clock
        self.max_pending = max_pending

    def verify(self, payload: bytes, *, topic: str | None = None) -> tuple[int, VerifiedReading]:
        if type(payload) is not bytes:
            raise TypeError('wire payload must be bytes')
        now = require_utc(self.clock())
        digest = hashlib.sha256(payload).digest()
        try:
            reading = decode_reading(payload)
        except ValueError:
            reading = None
        with self.registry.transaction() as db:
            if db.execute('SELECT COUNT(*) FROM events WHERE delivered=0').fetchone()[0] >= self.max_pending:
                raise BufferError('provenance outbox full; verification did not advance')
            status, signature, findings = 'malformed', None, ('invalid_cbor_envelope',)
            if reading is not None:
                identity = reading.identity[:3]
                key = db.execute('SELECT public,revoked FROM keys WHERE sensor=? AND key_id=?', identity[:2]).fetchone()
                head = db.execute('SELECT next_sequence,previous,timestamp FROM heads WHERE sensor=? AND key_id=? AND stream=?', identity).fetchone()
                if key is None:
                    status, findings = 'unknown_sensor', ('sensor_or_key_not_enrolled',)
                elif key[1]:
                    status, findings = 'revoked_key', ('key_revoked',)
                elif head is None:
                    status, findings = 'unknown_sensor', ('stream_not_enrolled',)
                else:
                    try:
                        Ed25519PublicKey.from_public_bytes(key[0]).verify(reading.signature, signing_bytes(reading))
                    except InvalidSignature:
                        status, signature, findings = 'bad_signature', False, ('ed25519_verification_failed',)
                    else:
                        signature = True
                        time_finding = self.policy.finding(reading.observed_at, now)
                        if topic is not None and topic != f'sensors/raw/{reading.sensor_id}':
                            status, findings = 'chain_anomaly', ('topic_identity_mismatch',)
                        elif reading.sequence < head[0]:
                            status, findings = 'replay', ('sequence_already_consumed',)
                        elif time_finding:
                            status, findings = 'out_of_window', (time_finding,)
                        elif reading.sequence > head[0]:
                            status, findings = 'gap', ('missing_predecessors',)
                        elif reading.prev_hash != head[1]:
                            status, findings = 'chain_anomaly', ('previous_hash_mismatch',)
                        elif head[2] is not None and reading.timestamp < head[2]:
                            status, findings = 'out_of_window', ('timestamp_regression',)
                        elif reading.sequence == 2**63 - 1:
                            status, findings = 'chain_anomaly', ('sequence_exhausted',)
                        else:
                            status, findings = 'valid', ()
                            db.execute('UPDATE heads SET next_sequence=?,previous=?,timestamp=? WHERE sensor=? AND key_id=? AND stream=?',
                                       (reading.sequence + 1, record_hash(reading), reading.timestamp, *identity))
            verdict = VerifiedReading(schema_version='1.0', reading=reading, status=status,
                                      signature_verified=signature, checked_at=now,
                                      findings=findings, raw_message_sha256=digest)
            event_id = db.execute('INSERT INTO events(verdict) VALUES (?)', (verdict.model_dump_json(),)).lastrowid
        return event_id, verdict

    def pending(self, limit: int = 128) -> tuple[tuple[int, VerifiedReading], ...]:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('invalid batch limit')
        with self.registry.transaction() as db:
            rows = db.execute('SELECT id,verdict FROM events WHERE delivered=0 ORDER BY id LIMIT ?', (limit,)).fetchall()
        return tuple((event_id, VerifiedReading.model_validate_json(value)) for event_id, value in rows)

    def acknowledge(self, event_id: int) -> None:
        with self.registry.transaction() as db:
            db.execute('UPDATE events SET delivered=1 WHERE id=?', (event_id,))
