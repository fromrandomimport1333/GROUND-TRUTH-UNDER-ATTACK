"""Read trusted local L1b evidence, including nulls/rejections absent from L2 callbacks."""
from core.contracts import VerifiedReading
from core.provenance.keyregistry import KeyRegistry


def read_verifier_journal(registry: KeyRegistry, *, limit: int = 10000):
    if type(limit) is not int or not 1 <= limit <= 20000:
        raise ValueError('journal bound must be 1..20000')
    with registry.transaction() as db:
        rows = db.execute('SELECT verdict FROM events ORDER BY id LIMIT ?', (limit+1,)).fetchall()
    if len(rows) > limit:
        raise BufferError('journal exceeds bound; select/archive an evaluation segment explicitly')
    return tuple(VerifiedReading.model_validate_json(row[0]) for row in rows)
