"""Protected-sink protocols the PW0 boundary guards (ADR-0018 D4, SPEC-PII scope).

These name the five sink categories the task requires a gate in front of: ordinary
persistence and each stage of parse/chunk/embed/projection. They do not implement or
wire the real Postgres/Neo4j/Qdrant/parser/embedder adapters -- SPEC-PII scopes
"changing current ingestion runtime through this draft" out; production wiring is a
separate, later change against the real adapters in ``omniscience_server.ingestion``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from omniscience_core.privacy import DataEnvelope


@runtime_checkable
class OrdinaryStoreSink(Protocol):
    def store(self, envelope: DataEnvelope) -> None: ...


@runtime_checkable
class ParseSink(Protocol):
    def parse(self, envelope: DataEnvelope) -> None: ...


@runtime_checkable
class ChunkSink(Protocol):
    def chunk(self, envelope: DataEnvelope) -> None: ...


@runtime_checkable
class EmbedSink(Protocol):
    def embed(self, envelope: DataEnvelope) -> None: ...


@runtime_checkable
class ProjectionSink(Protocol):
    def project(self, envelope: DataEnvelope) -> None: ...
