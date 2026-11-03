"""Offline replay of recorded wire bytes through the production L1b/L2 code."""
from dataclasses import dataclass
from datetime import datetime

from pydantic import Field, StrictBytes
from core.contracts import Contract, UtcDatetime, VerifiedReading
from core.pipeline import ProvenancePipeline, WindowPipeline
from core.processing.engine import ProcessingEngine, WindowResult, ExcludedEvent, bounded
from core.provenance.replay_protection import ReplayPolicy, require_utc
from core.provenance.verifier import Verifier


class ReplayRecord(Contract):
    payload: StrictBytes = Field(max_length=65536)
    received_at: UtcDatetime
    topic: str | None = Field(default=None, max_length=256)


@dataclass(frozen=True)
class ReplayResult:
    events: tuple[VerifiedReading, ...]
    windows: tuple[WindowResult, ...]
    unassigned: tuple[ExcludedEvent, ...]


class ReplayAdapter:
    """Single-use isolated evaluation; never rewinds a live registry.

    Enroll the recording's public keys/streams in a NEW registry before running.
    Original signed timestamps and wire bytes are retained. received_at controls
    the historical verifier clock; as_of controls window availability. No sleep.
    Supply the same recording, policy, registry enrollment and references to
    reproduce outputs. A failed run requires a new evaluation registry.
    """
    def __init__(self, registry, *, layer4, layer3, processing_policy=None, replay_policy=None):
        self.registry = registry
        self.layer4, self.layer3 = layer4, layer3
        self.engine = ProcessingEngine(processing_policy)
        self.policy = replay_policy or ReplayPolicy()
        self.used = False

    def run(self, records, *, as_of: datetime, references=()) -> ReplayResult:
        if self.used:
            raise RuntimeError('replay adapter is single-use; create a fresh evaluation registry')
        as_of = require_utc(as_of)
        records = tuple(ReplayRecord.model_validate(r) for r in bounded(records, self.engine.policy.max_events, 'replay batch'))
        refs = tuple(bounded(references, self.engine.policy.max_references, 'reference batch'))
        if any(a.received_at > b.received_at for a, b in zip(records, records[1:])):
            raise ValueError('arrival times must be nondecreasing; payload order is preserved')
        if any(r.received_at > as_of for r in records):
            raise ValueError('as_of precedes an arrival')
        with self.registry.transaction() as db:
            if db.execute('SELECT COUNT(*) FROM events').fetchone()[0] or db.execute('SELECT COUNT(*) FROM heads WHERE next_sequence != 0').fetchone()[0]:
                raise ValueError('replay requires a fresh evaluation registry')
        self.used = True
        now = as_of
        verifier = Verifier(self.registry, policy=self.policy, clock=lambda: now)
        provenance = ProvenancePipeline(verifier, self.layer4)
        events = []
        for record in records:
            now = record.received_at
            events.append(provenance.receive(record.payload, topic=record.topic))
        windows, unassigned = WindowPipeline(self.engine, self.layer3).process(events, as_of=as_of, references=refs)
        return ReplayResult(tuple(events), windows, unassigned)
