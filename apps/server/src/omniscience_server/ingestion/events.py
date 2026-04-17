"""Event types for the ingestion pipeline.

``DocumentChangeEvent`` is the payload consumed from the NATS
``INGEST_CHANGES`` stream.  ``ProcessResult`` captures the outcome of
running a single document through the pipeline.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class DocumentChangeEvent(BaseModel):
    """Payload emitted by source connectors on every document change.

    Published to ``ingest.changes.<source_type>`` and consumed by
    :class:`~omniscience_server.ingestion.worker.IngestionWorker`.
    """

    source_id: UUID
    """Database PK of the :class:`~omniscience_core.db.models.Source` row."""

    source_type: str
    """Connector type string (e.g. ``"git"``, ``"fs"``)."""

    external_id: str
    """Source-native document identifier — stable across syncs."""

    uri: str
    """Human-readable address for the document (URL, file path, …)."""

    action: Literal["created", "updated", "deleted"]
    """Change type as reported by the connector."""


class ProcessResult(BaseModel):
    """Outcome of processing a single :class:`DocumentChangeEvent`.

    Returned by :meth:`~omniscience_server.ingestion.pipeline.IngestionPipeline.run`
    and used by :class:`~omniscience_server.ingestion.worker.IngestionWorker`
    to update run counters and emit metrics.
    """

    source_id: UUID
    external_id: str
    action: str
    """Effective action: ``created``, ``updated``, ``unchanged``, ``deleted``, or ``error``."""

    duration_ms: float
    """Wall-clock time to process this document, in milliseconds."""

    error: str | None = None
    """Human-readable error message when ``action == "error"``."""


__all__ = ["DocumentChangeEvent", "ProcessResult"]
