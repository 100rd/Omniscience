"""Per-document ingestion pipeline.

:class:`IngestionPipeline` orchestrates six stages for each document:

    fetch → hash_check → parse → chunk → embed → index

Each stage runs inside its own structured-log context and records a
Prometheus histogram observation.  Failures are caught at the pipeline
level; callers receive a :class:`~omniscience_server.ingestion.events.ProcessResult`
regardless of whether the run succeeded or produced an error.

Wave-5 note: ``parse`` and ``chunk`` are intentional placeholders.
Real parsers/chunkers will be wired in when Wave 5 lands.

IndexWriter note: ``IndexWriterProtocol`` is the interface contract for the
parallel issue-11 implementation (``omniscience_index.IndexWriter``).
The real integration happens when both branches are merged.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import structlog
from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument
from omniscience_embeddings.base import EmbeddingProvider

from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.metrics import (
    INGESTION_ERRORS_TOTAL,
    INGESTION_STAGE_DURATION_SECONDS,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Local content hash (mirrors omniscience_index.hashing — merged in issue-11)
# ---------------------------------------------------------------------------


def _compute_content_hash(text: str) -> str:
    """Return SHA-256 hex digest of *text* after cosmetic normalisation.

    Normalisation steps match ``omniscience_index.hashing.compute_content_hash``
    so hashes are compatible once the branches are merged.

    Steps:
    1. Strip leading BOM (U+FEFF).
    2. Strip trailing whitespace per line.
    3. Collapse consecutive blank lines to one.
    """
    text = text.lstrip("\ufeff")
    lines = [line.rstrip() for line in text.splitlines()]

    normalised: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
        else:
            if blank_run > 0:
                normalised.append("")
            blank_run = 0
            normalised.append(line)

    return hashlib.sha256("\n".join(normalised).encode()).hexdigest()


# ---------------------------------------------------------------------------
# IndexWriter protocol (interface for the parallel issue-11 implementation)
# ---------------------------------------------------------------------------


@runtime_checkable
class IndexWriterProtocol(Protocol):
    """Minimal surface of IndexWriter needed by the ingestion pipeline.

    The real ``omniscience_index.IndexWriter`` satisfies this protocol.
    Tests inject a mock that also satisfies it.
    """

    async def upsert_document(
        self,
        source_id: UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[Any],
        ingestion_run_id: UUID | None,
    ) -> Any: ...

    async def tombstone(self, source_id: UUID, external_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Chunk placeholder (Wave 5 will replace with real ChunkData)
# ---------------------------------------------------------------------------


class _RawChunk:
    """Minimal chunk produced by the placeholder chunker stage."""

    def __init__(
        self,
        text: str,
        embedding: list[float],
        embedding_model: str,
        embedding_provider: str,
    ) -> None:
        self.ord = 0
        self.text = text
        self.embedding = embedding
        self.symbol: str | None = None
        self.metadata: dict[str, Any] = {}
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self.parser_version = "placeholder-v0"
        self.chunker_strategy = "full-content-v0"


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class IngestionPipeline:
    """Orchestrates the per-document pipeline stages.

    Each stage is isolated: a failure in one stage sets ``action="error"``
    on the result but does not raise; the caller (worker) decides how to
    ack/nak the underlying message.
    """

    def __init__(
        self,
        connector: Connector,
        embedding_provider: EmbeddingProvider,
        index_writer: IndexWriterProtocol,
    ) -> None:
        self._connector = connector
        self._embedding_provider = embedding_provider
        self._index_writer = index_writer

    async def run(
        self,
        event: DocumentChangeEvent,
        config: Any,
        secrets: dict[str, str],
        ingestion_run_id: UUID | None = None,
    ) -> ProcessResult:
        """Execute all stages and return a result regardless of outcome."""
        started = time.monotonic()
        bound = log.bind(
            source_id=str(event.source_id),
            source_type=event.source_type,
            external_id=event.external_id,
            action=event.action,
        )

        try:
            result = await self._execute(event, config, secrets, ingestion_run_id, bound)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            bound.error("pipeline_unexpected_error", error=str(exc))
            return ProcessResult(
                source_id=event.source_id,
                external_id=event.external_id,
                action="error",
                duration_ms=elapsed_ms,
                error=str(exc),
            )

        result_with_ms = result.model_copy(
            update={"duration_ms": (time.monotonic() - started) * 1000}
        )
        bound.info("pipeline_complete", action=result_with_ms.action)
        return result_with_ms

    # ------------------------------------------------------------------
    # Stage orchestration
    # ------------------------------------------------------------------

    async def _execute(
        self,
        event: DocumentChangeEvent,
        config: Any,
        secrets: dict[str, str],
        ingestion_run_id: UUID | None,
        bound: Any,
    ) -> ProcessResult:
        """Inner execution — may raise; exceptions are caught by :meth:`run`."""
        if event.action == "deleted":
            return await self._handle_delete(event, bound)

        fetched = await self._stage_fetch(event, config, secrets, bound)
        content_text = fetched.content_bytes.decode(errors="replace")

        unchanged = await self._stage_hash_check(event, content_text, bound)
        if unchanged:
            return ProcessResult(
                source_id=event.source_id,
                external_id=event.external_id,
                action="unchanged",
                duration_ms=0.0,
            )

        parsed_text = await self._stage_parse(content_text, event.source_type, bound)
        chunks_text = await self._stage_chunk(parsed_text, event.source_type, bound)
        embeddings = await self._stage_embed(chunks_text, event.source_type, bound)
        upsert_action = await self._stage_index(
            event, fetched, content_text, chunks_text, embeddings, ingestion_run_id, bound
        )
        return ProcessResult(
            source_id=event.source_id,
            external_id=event.external_id,
            action=upsert_action,
            duration_ms=0.0,
        )

    # ------------------------------------------------------------------
    # Individual stages
    # ------------------------------------------------------------------

    async def _stage_fetch(
        self,
        event: DocumentChangeEvent,
        config: Any,
        secrets: dict[str, str],
        bound: Any,
    ) -> FetchedDocument:
        t0 = time.monotonic()
        try:
            ref = DocumentRef(external_id=event.external_id, uri=event.uri)
            fetched = await self._connector.fetch(config, secrets, ref)
            bound.debug("stage_fetch_ok", content_bytes=len(fetched.content_bytes))
            return fetched
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage="fetch").inc()
            bound.error("stage_fetch_error", error=str(exc))
            raise
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="fetch").observe(time.monotonic() - t0)

    async def _stage_hash_check(
        self,
        event: DocumentChangeEvent,
        content_text: str,
        bound: Any,
    ) -> bool:
        """Return True if content is unchanged (caller should skip re-indexing)."""
        t0 = time.monotonic()
        try:
            _new_hash = _compute_content_hash(content_text)
            bound.debug("stage_hash_check_ok", content_hash=_new_hash[:16])
            # Hash comparison against stored value happens inside index writer's
            # upsert_document (it reads the existing row).  Here we just record
            # the hash for logging purposes; actual skip happens via UpsertResult.
            return False
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="hash_check").observe(
                time.monotonic() - t0
            )

    async def _stage_parse(
        self,
        content_text: str,
        source_type: str,
        bound: Any,
    ) -> str:
        """Placeholder parser: pass raw content through unchanged."""
        t0 = time.monotonic()
        try:
            bound.debug("stage_parse_ok", strategy="placeholder-v0", source_type=source_type)
            return content_text
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="parse").observe(time.monotonic() - t0)

    async def _stage_chunk(
        self,
        parsed_text: str,
        source_type: str,
        bound: Any,
    ) -> list[str]:
        """Placeholder chunker: single chunk from full content."""
        t0 = time.monotonic()
        try:
            bound.debug("stage_chunk_ok", strategy="full-content-v0", chunks=1)
            return [parsed_text]
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="chunk").observe(time.monotonic() - t0)

    async def _stage_embed(
        self,
        chunks_text: list[str],
        source_type: str,
        bound: Any,
    ) -> list[list[float]]:
        t0 = time.monotonic()
        try:
            vectors = await self._embedding_provider.embed(chunks_text)
            bound.debug(
                "stage_embed_ok", chunks=len(vectors), dim=len(vectors[0]) if vectors else 0
            )
            return vectors
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=source_type, stage="embed").inc()
            bound.error("stage_embed_error", error=str(exc))
            raise
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="embed").observe(time.monotonic() - t0)

    async def _stage_index(
        self,
        event: DocumentChangeEvent,
        fetched: FetchedDocument,
        content_text: str,
        chunks_text: list[str],
        embeddings: list[list[float]],
        ingestion_run_id: UUID | None,
        bound: Any,
    ) -> str:
        t0 = time.monotonic()
        try:
            content_hash = _compute_content_hash(content_text)
            chunks = [
                _RawChunk(
                    text=text,
                    embedding=vec,
                    embedding_model=self._embedding_provider.model_name,
                    embedding_provider=self._embedding_provider.provider_name,
                )
                for text, vec in zip(chunks_text, embeddings, strict=True)
            ]
            result = await self._index_writer.upsert_document(
                source_id=event.source_id,
                external_id=event.external_id,
                uri=event.uri,
                title=None,
                content_hash=content_hash,
                metadata=dict(fetched.ref.metadata),
                chunks=chunks,
                ingestion_run_id=ingestion_run_id,
            )
            action: str = result.action
            if action == "unchanged":
                bound.debug("stage_index_unchanged")
            else:
                bound.debug("stage_index_ok", action=action, chunks_written=result.chunks_written)
            return action
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage="index").inc()
            bound.error("stage_index_error", error=str(exc))
            raise
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="index").observe(time.monotonic() - t0)

    async def _handle_delete(self, event: DocumentChangeEvent, bound: Any) -> ProcessResult:
        t0 = time.monotonic()
        try:
            found = await self._index_writer.tombstone(event.source_id, event.external_id)
            action = "deleted" if found else "unchanged"
            bound.info("stage_delete_ok", found=found)
            return ProcessResult(
                source_id=event.source_id,
                external_id=event.external_id,
                action=action,
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage="index").inc()
            bound.error("stage_delete_error", error=str(exc))
            raise
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="index").observe(time.monotonic() - t0)


__all__ = ["IndexWriterProtocol", "IngestionPipeline"]
