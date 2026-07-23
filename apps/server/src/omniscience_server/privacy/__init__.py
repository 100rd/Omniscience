"""Server-side wiring for the PW0 fail-closed PII boundary (task-sp-61-pii-wall-pw0)."""

from __future__ import annotations

from omniscience_server.privacy.boundary import Pw0Boundary, Pw0BoundaryDeniedError
from omniscience_server.privacy.sinks import (
    ChunkSink,
    EmbedSink,
    OrdinaryStoreSink,
    ParseSink,
    ProjectionSink,
)

__all__ = [
    "ChunkSink",
    "EmbedSink",
    "OrdinaryStoreSink",
    "ParseSink",
    "ProjectionSink",
    "Pw0Boundary",
    "Pw0BoundaryDeniedError",
]
