"""Ingestion pipeline package.

Public surface:

    from omniscience_server.ingestion import (
        IngestionWorker,
        IngestionPipeline,
        DocumentChangeEvent,
        ProcessResult,
    )
"""

from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.pipeline import IndexWriterProtocol, IngestionPipeline
from omniscience_server.ingestion.worker import IngestionWorker

__all__ = [
    "DocumentChangeEvent",
    "IndexWriterProtocol",
    "IngestionPipeline",
    "IngestionWorker",
    "ProcessResult",
]
