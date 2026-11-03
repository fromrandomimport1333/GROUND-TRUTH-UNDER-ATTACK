"""Provenance routing and the direct Layer 2 to Layer 3 window boundary."""
from typing import Callable
from core.contracts import VerifiedReading
from core.provenance.verifier import Verifier


class ProvenancePipeline:
    """At-least-once delivery. Consumers must deduplicate stable local event IDs.

    A consumer exception leaves the durable event pending. One dispatcher per
    database is supported. An L4 sink is required; L2 is optional in Phase 3.
    """
    def __init__(self, verifier: Verifier, layer4: Callable[[int, VerifiedReading], None],
                 layer2: Callable[[int, VerifiedReading], None] | None = None, *,
                 include_ineligible: bool = False):
        self.verifier, self.layer4, self.layer2 = verifier, layer4, layer2
        self.include_ineligible = include_ineligible

    def drain(self) -> int:
        count = 0
        for event_id, verdict in self.verifier.pending():
            self.layer4(event_id, verdict)
            if self.layer2 is not None and (verdict.analysis_eligible or self.include_ineligible):
                self.layer2(event_id, verdict)
            self.verifier.acknowledge(event_id)
            count += 1
        return count

    def receive(self, payload: bytes, *, topic: str | None = None) -> VerifiedReading:
        _, verdict = self.verifier.verify(payload, topic=topic)
        self.drain()
        return verdict


class WindowPipeline:
    """Synchronous L2 -> L3 boundary using the shared FeatureWindow contract.

    Failures propagate. Callers retain WindowResult and retry deliver explicitly;
    consumers deduplicate window_id. No claim of durable/exactly-once L3 delivery.
    Raw provenance events still go to L4 through ProvenancePipeline independently.
    """
    def __init__(self, engine, layer3):
        if not callable(layer3):
            raise TypeError('Layer 3 must be a callable accepting FeatureWindow')
        self.engine, self.layer3 = engine, layer3

    def deliver(self, window):
        from core.contracts import FeatureWindow
        if not isinstance(window, FeatureWindow):
            raise TypeError('Layer 3 input must be FeatureWindow')
        self.layer3(FeatureWindow.model_validate(window))

    def process(self, events, *, as_of, references=()):
        results, unassigned = self.engine.build_closed_windows(events, as_of=as_of, references=references)
        for result in results:
            self.deliver(result.window)
        return results, unassigned
