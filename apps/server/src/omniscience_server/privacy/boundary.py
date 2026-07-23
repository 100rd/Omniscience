"""Pw0Boundary -- wires ``IngestPIIGate`` decisions to protected sink calls.

The boundary refuses to invoke a sink itself unless the supplied ``GateDecision`` is
``ADMIT``; it never re-evaluates or trusts caller discipline. This is the mechanism
behind "PW0 blocks or sanitizes before every ordinary store and every
parse/chunk/embed/projection sink" (task-sp-61-pii-wall-pw0).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from omniscience_core.privacy import DataEnvelope, GateDecision, GateDisposition

from omniscience_server.privacy.sinks import (
    ChunkSink,
    EmbedSink,
    OrdinaryStoreSink,
    ParseSink,
    ProjectionSink,
)


class Pw0BoundaryDeniedError(Exception):
    """Raised when a caller attempts to invoke a protected sink with a decision that
    did not admit. The boundary never runs the sink call in that case."""


@dataclass(frozen=True)
class Pw0Boundary:
    def guard_store(
        self, decision: GateDecision, sink: OrdinaryStoreSink, envelope: DataEnvelope
    ) -> None:
        self._guard(decision, lambda: sink.store(envelope))

    def guard_parse(self, decision: GateDecision, sink: ParseSink, envelope: DataEnvelope) -> None:
        self._guard(decision, lambda: sink.parse(envelope))

    def guard_chunk(self, decision: GateDecision, sink: ChunkSink, envelope: DataEnvelope) -> None:
        self._guard(decision, lambda: sink.chunk(envelope))

    def guard_embed(self, decision: GateDecision, sink: EmbedSink, envelope: DataEnvelope) -> None:
        self._guard(decision, lambda: sink.embed(envelope))

    def guard_project(
        self, decision: GateDecision, sink: ProjectionSink, envelope: DataEnvelope
    ) -> None:
        self._guard(decision, lambda: sink.project(envelope))

    @staticmethod
    def _guard(decision: GateDecision, call: Callable[[], None]) -> None:
        if decision.disposition != GateDisposition.ADMIT:
            raise Pw0BoundaryDeniedError(decision.reason)
        call()
