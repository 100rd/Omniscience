"""Per-document ingestion pipeline.

:class:`IngestionPipeline` orchestrates the per-document write path:

    fetch -> hash_check -> parse -> chunk -> embed
        -> index (Postgres metadata)
        -> vector (Qdrant chunk embeddings)
        -> graph (Neo4j entities + edges)
        -> link (cross-source linker, optional)

Each stage runs inside its own structured-log context and records a
Prometheus histogram observation.  Failures are caught at the pipeline
level; callers receive a :class:`~omniscience_server.ingestion.events.ProcessResult`
regardless of whether the run succeeded or produced an error.

Three-store fan-out (issue #126)
--------------------------------

The cutover in PR #125 left the live worker writing only Postgres
operational metadata; this pipeline restores the invariant that an
ingested document is queryable in all three stores:

- :class:`~omniscience_index.IndexWriter`   -> Postgres metadata.
- :class:`~omniscience_core.storage.GraphStore`  -> Neo4j entities/edges.
- :class:`~omniscience_core.storage.VectorStore` -> Qdrant chunk vectors.

The Postgres write happens first because it produces the canonical
``document_id`` carried into Neo4j logs and matches what
``IndexWriter`` already returns.  The Neo4j and Qdrant adapters are
both idempotent (MERGE on Neo4j, content-hash dedup on Qdrant) so any
retry triggered by a partial failure is safe.

ACL invariant (non-negotiable, ADR-0006 §ACL / ADR-0005 §ACL)
-------------------------------------------------------------

Every stage that talks to Neo4j or Qdrant carries an explicit
``workspace_id`` resolved by the worker BEFORE the pipeline runs (see
:mod:`omniscience_index.workspace`).  An unresolved workspace causes
the worker to bail out before the pipeline starts; the pipeline
itself never sees a ``None`` workspace.

Best-effort vs hard failures
----------------------------

- **Parser/extractor failures** in ``_stage_graph`` are best-effort and
  swallowed (existing behaviour preserved).  Tree-sitter not
  recognising the language, or an extractor exception, must not abort
  ingestion.
- **Adapter failures** (Neo4j Cypher error, Qdrant gRPC error,
  ``MissingWorkspaceError`` raised inside an adapter) propagate up so
  the worker NAKs the message and the broker redelivers.  Idempotence
  on every adapter makes redelivery safe.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import structlog
from omniscience_connectors.base import Connector, DocumentRef, FetchedDocument
from omniscience_core.storage.graph import GraphStore
from omniscience_core.storage.vector import ChunkPayload, VectorStore
from omniscience_embeddings.base import EmbeddingProvider

from omniscience_server.ingestion.events import DocumentChangeEvent, ProcessResult
from omniscience_server.ingestion.metrics import (
    INGESTION_ERRORS_TOTAL,
    INGESTION_STAGE_DURATION_SECONDS,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants — every magic number lives here with a rationale.
# ---------------------------------------------------------------------------

# Parser/chunker version stamps written to the Postgres chunk row + the
# Qdrant payload (ADR-0006 §Schema).  Bump these when the placeholder
# parser/chunker is replaced; downstream queries key on these values to
# segment results by extraction strategy.
_PARSER_VERSION: str = "placeholder-v0"
_CHUNKER_STRATEGY: str = "full-content-v0"

# Stage label used in metrics for the new Qdrant write.  Kept distinct
# from the existing ``index`` label (which means "Postgres index").
_STAGE_VECTOR: str = "vector"


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
# IndexWriter protocol (interface for the Postgres metadata writer)
# ---------------------------------------------------------------------------


@runtime_checkable
class IndexWriterProtocol(Protocol):
    """Minimal surface of IndexWriter needed by the ingestion pipeline.

    The real ``omniscience_index.IndexWriter`` satisfies this protocol.
    Tests inject a mock that also satisfies it.

    Note: ``upsert_graph`` is intentionally NOT on this protocol any more
    (issue #126) — graph writes go to the Neo4j adapter via
    ``GraphStore.upsert_graph``.  The Postgres writer keeps a method by
    that name for the legacy migration codepath, but ingestion no longer
    reaches it.
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
# EntityLinker protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EntityLinkerProtocol(Protocol):
    """Minimal surface of EntityLinker needed by the ingestion pipeline.

    The real ``omniscience_index.EntityLinker`` satisfies this protocol.
    Tests inject a mock that also satisfies it.
    """

    async def link_entities(self, source_id: UUID) -> int: ...


# ---------------------------------------------------------------------------
# Chunk record produced by the placeholder chunker stage
# ---------------------------------------------------------------------------


class _RawChunk:
    """Minimal chunk produced by the placeholder chunker stage.

    The Postgres index keeps everything except the embedding vector;
    Qdrant gets a separate :class:`ChunkPayload` TypedDict built from
    these fields plus the embedding (see :func:`_chunk_to_payload`).
    """

    def __init__(
        self,
        ord_: int,
        text: str,
        embedding: list[float],
        embedding_model: str,
        embedding_provider: str,
    ) -> None:
        self.ord = ord_
        self.text = text
        self.symbol: str | None = None
        self.metadata: dict[str, Any] = {}
        self.embedding = embedding
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self.parser_version = _PARSER_VERSION
        self.chunker_strategy = _CHUNKER_STRATEGY


def _chunk_to_payload(chunk: _RawChunk) -> ChunkPayload:
    """Translate a pipeline ``_RawChunk`` into a Qdrant ``ChunkPayload``.

    Mirrors the migration runner's ``_chunk_to_payload`` so the live
    ingestion path produces the exact same payload shape as a one-off
    backfill.  Lineage fields are propagated per ADR-0006 §Schema.
    """
    return {
        "ord": int(chunk.ord),
        "text": str(chunk.text),
        "embedding": [float(x) for x in chunk.embedding],
        "symbol": chunk.symbol,
        "metadata": dict(chunk.metadata),
        "embedding_model": str(chunk.embedding_model),
        "embedding_provider": str(chunk.embedding_provider),
        "parser_version": str(chunk.parser_version),
        "chunker_strategy": str(chunk.chunker_strategy),
    }


def _build_vector_metadata(
    *,
    workspace_id: uuid.UUID,
    fetched_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build the document-level metadata dict for the Qdrant adapter.

    The ``workspace_id`` key is mandatory — it is the ACL enforcement
    point inside ``QdrantVectorStore._extract_workspace_id`` (ADR-0006
    §ACL).  Mirrors ``migration.runner._build_document_metadata``.
    """
    metadata: dict[str, Any] = dict(fetched_metadata)
    metadata["workspace_id"] = str(workspace_id)
    return metadata


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class IngestionPipeline:
    """Orchestrates the per-document pipeline stages.

    Each stage is isolated: a failure in a hard stage sets
    ``action="error"`` on the result but does not raise; the caller
    (worker) decides how to ack/nak the underlying message.

    Symbol graph extraction runs as an optional stage after the vector
    write.  It is activated when both ``_graph_extractor`` is provided
    and the parsed document's language is supported.  Parser/extractor
    errors are swallowed (best-effort); **adapter** errors from
    ``GraphStore.upsert_graph`` propagate up so the broker redelivers.

    Entity linking runs as an optional stage after graph extraction.
    Linking failures are logged and swallowed — they never abort
    indexing (the linker runs over many sources at once and a transient
    DB hiccup must not poison a single document's ingestion).
    """

    def __init__(
        self,
        connector: Connector,
        embedding_provider: EmbeddingProvider,
        index_writer: IndexWriterProtocol,
        graph_store: GraphStore,
        vector_store: VectorStore,
        graph_extractor: Any | None = None,
        entity_linker: EntityLinkerProtocol | None = None,
    ) -> None:
        self._connector = connector
        self._embedding_provider = embedding_provider
        self._index_writer = index_writer
        self._graph_store = graph_store
        self._vector_store = vector_store
        # Optional callable:
        #   graph_extractor(parsed_doc, source_bytes) -> (entities, edges)
        # Matches the signature of omniscience_parsers.code.graph.extract_symbol_graph
        self._graph_extractor = graph_extractor
        # Optional EntityLinker (or compatible duck-typed object)
        self._entity_linker = entity_linker

    async def run(
        self,
        event: DocumentChangeEvent,
        config: Any,
        secrets: dict[str, str],
        workspace_id: uuid.UUID,
        ingestion_run_id: UUID | None = None,
    ) -> ProcessResult:
        """Execute all stages and return a result regardless of outcome.

        ``workspace_id`` is required and resolved by the caller (the
        worker) before this method runs.  It is never optional — the
        ACL invariant on Neo4j/Qdrant is non-negotiable.
        """
        started = time.monotonic()
        bound = log.bind(
            source_id=str(event.source_id),
            source_type=event.source_type,
            external_id=event.external_id,
            action=event.action,
            workspace_id=str(workspace_id),
        )

        try:
            result = await self._execute(
                event, config, secrets, workspace_id, ingestion_run_id, bound
            )
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
        workspace_id: uuid.UUID,
        ingestion_run_id: UUID | None,
        bound: Any,
    ) -> ProcessResult:
        """Inner execution — may raise; exceptions are caught by :meth:`run`."""
        if event.action == "deleted":
            return await self._handle_delete(event, workspace_id, bound)

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
        chunks = self._build_chunks(chunks_text, embeddings)

        upsert_action, document_id = await self._stage_index(
            event, fetched, content_text, chunks, ingestion_run_id, bound
        )

        # Vector write (Qdrant) — hard failure propagates to caller.
        await self._stage_vector(
            event=event,
            fetched=fetched,
            content_hash=_compute_content_hash(content_text),
            chunks=chunks,
            workspace_id=workspace_id,
            ingestion_run_id=ingestion_run_id,
            bound=bound,
        )

        # Symbol graph extraction — parser/extractor errors swallowed,
        # adapter errors propagate.
        await self._stage_graph(
            event=event,
            content_bytes=fetched.content_bytes,
            document_id=document_id,
            workspace_id=workspace_id,
            bound=bound,
        )

        # Cross-source entity linking — optional, best-effort
        await self._stage_link(event, bound)

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
            bound.debug("stage_parse_ok", strategy=_PARSER_VERSION, source_type=source_type)
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
            bound.debug("stage_chunk_ok", strategy=_CHUNKER_STRATEGY, chunks=1)
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

    def _build_chunks(
        self,
        chunks_text: list[str],
        embeddings: list[list[float]],
    ) -> list[_RawChunk]:
        """Pair chunk text with embedding vectors at matching ordinals.

        Pulled out of ``_stage_index`` so the same chunk objects are
        passed to both Postgres and Qdrant — guarantees the two stores
        agree on chunk count and embedding lineage.
        """
        if len(chunks_text) != len(embeddings):
            raise ValueError(
                f"chunk_embedding_mismatch: {len(chunks_text)} chunks vs "
                f"{len(embeddings)} embeddings"
            )
        return [
            _RawChunk(
                ord_=i,
                text=text,
                embedding=embedding,
                embedding_model=self._embedding_provider.model_name,
                embedding_provider=self._embedding_provider.provider_name,
            )
            for i, (text, embedding) in enumerate(zip(chunks_text, embeddings, strict=True))
        ]

    async def _stage_index(
        self,
        event: DocumentChangeEvent,
        fetched: FetchedDocument,
        content_text: str,
        chunks: list[_RawChunk],
        ingestion_run_id: UUID | None,
        bound: Any,
    ) -> tuple[str, UUID]:
        """Write chunks to the Postgres index.  Returns (action, document_id)."""
        t0 = time.monotonic()
        try:
            content_hash = _compute_content_hash(content_text)
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
            document_id: UUID = result.document_id
            if action == "unchanged":
                bound.debug("stage_index_unchanged")
            else:
                bound.debug("stage_index_ok", action=action, chunks_written=result.chunks_written)
            return action, document_id
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage="index").inc()
            bound.error("stage_index_error", error=str(exc))
            raise
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="index").observe(time.monotonic() - t0)

    async def _stage_vector(
        self,
        *,
        event: DocumentChangeEvent,
        fetched: FetchedDocument,
        content_hash: str,
        chunks: list[_RawChunk],
        workspace_id: uuid.UUID,
        ingestion_run_id: UUID | None,
        bound: Any,
    ) -> None:
        """Push chunk embeddings to Qdrant.  Hard failure propagates."""
        t0 = time.monotonic()
        try:
            payloads = [_chunk_to_payload(c) for c in chunks]
            metadata = _build_vector_metadata(
                workspace_id=workspace_id,
                fetched_metadata=dict(fetched.ref.metadata),
            )
            outcome = await self._vector_store.upsert_chunks(
                source_id=event.source_id,
                external_id=event.external_id,
                uri=event.uri,
                title=None,
                content_hash=content_hash,
                metadata=metadata,
                chunks=payloads,
                ingestion_run_id=ingestion_run_id,
            )
            bound.debug(
                "stage_vector_ok",
                action=outcome.action,
                chunks_written=outcome.chunks_written,
                document_id=str(outcome.document_id),
            )
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage=_STAGE_VECTOR).inc()
            bound.error("stage_vector_error", error=str(exc))
            raise
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage=_STAGE_VECTOR).observe(
                time.monotonic() - t0
            )

    async def _stage_graph(
        self,
        *,
        event: DocumentChangeEvent,
        content_bytes: bytes,
        document_id: UUID,
        workspace_id: uuid.UUID,
        bound: Any,
    ) -> None:
        """Extract and persist the symbol graph.

        Two-tier failure handling:
        - Parser / extractor problems are best-effort (logged + swallowed).
        - Adapter problems on ``GraphStore.upsert_graph`` propagate up so
          the broker NAKs the message.  Idempotent MERGE on the Neo4j
          side makes redelivery safe.
        """
        if self._graph_extractor is None:
            return

        t0 = time.monotonic()
        try:
            extracted = self._extract_graph(event, content_bytes, bound)
            if extracted is None:
                return
            entities, edges = extracted
            await self._upsert_graph_with_acl(
                event=event,
                document_id=document_id,
                workspace_id=workspace_id,
                entities=entities,
                edges=edges,
                bound=bound,
            )
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="graph").observe(time.monotonic() - t0)

    def _extract_graph(
        self,
        event: DocumentChangeEvent,
        content_bytes: bytes,
        bound: Any,
    ) -> tuple[list[Any], list[Any]] | None:
        """Run the parser + extractor.  Best-effort: returns ``None`` on miss.

        Failures here are NOT propagated — extraction is non-critical
        (the document is still queryable via Postgres + Qdrant even
        without a symbol graph).  Returning ``None`` tells the caller
        to skip the adapter call.
        """
        try:
            from omniscience_parsers import TreeSitterParser

            parser = TreeSitterParser()
            ext = "." + event.external_id.rsplit(".", 1)[-1] if "." in event.external_id else ""
            if not parser.can_handle("", ext):
                return None

            parsed = parser.parse(content_bytes, file_path=event.external_id)
            entities, edges = self._graph_extractor(parsed, content_bytes)  # type: ignore[misc]
            if not entities and not edges:
                return None
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(
                source_type=event.source_type, stage="graph_extract"
            ).inc()
            bound.warning("stage_graph_extract_error", error=str(exc))
            return None
        return list(entities), list(edges)

    async def _upsert_graph_with_acl(
        self,
        *,
        event: DocumentChangeEvent,
        document_id: UUID,
        workspace_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        bound: Any,
    ) -> None:
        """Tag entities with ``workspace_id`` and call the Neo4j adapter.

        The adapter's :meth:`Neo4jGraphStore.upsert_graph` reads the
        workspace from the first entity (or its metadata) — see
        ``_workspace_from_entities`` in the adapter.  We tag every
        entity defensively so the adapter's lookup is unambiguous.

        Adapter errors propagate so the worker NAKs the message.
        """
        try:
            tagged = [_tag_entity_workspace(ent, workspace_id) for ent in entities]
            await self._graph_store.upsert_graph(
                source_id=event.source_id,
                document_id=document_id,
                entities=tagged,
                edges=edges,
            )
            bound.debug("stage_graph_ok", entities=len(entities), edges=len(edges))
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage="graph").inc()
            bound.error("stage_graph_adapter_error", error=str(exc))
            raise

    async def _stage_link(
        self,
        event: DocumentChangeEvent,
        bound: Any,
    ) -> None:
        """Create cross-source entity links.  Best-effort: never raises."""
        if self._entity_linker is None:
            return

        t0 = time.monotonic()
        try:
            new_edges = await self._entity_linker.link_entities(event.source_id)
            bound.debug("stage_link_ok", new_edges=new_edges)
        except Exception as exc:
            INGESTION_ERRORS_TOTAL.labels(source_type=event.source_type, stage="link").inc()
            bound.warning("stage_link_error", error=str(exc))
            # Swallow: entity linking is non-critical
        finally:
            INGESTION_STAGE_DURATION_SECONDS.labels(stage="link").observe(time.monotonic() - t0)

    async def _handle_delete(
        self,
        event: DocumentChangeEvent,
        workspace_id: uuid.UUID,
        bound: Any,
    ) -> ProcessResult:
        """Tombstone the document in Postgres and Qdrant.

        Neo4j entities are not actively tombstoned per-document — the
        next re-ingestion of any document for the same source replaces
        the source's graph slice via :meth:`Neo4jGraphStore.upsert_graph`
        (DETACH DELETE by source_id).  This matches the migration
        runner's semantics.
        """
        t0 = time.monotonic()
        try:
            found = await self._index_writer.tombstone(event.source_id, event.external_id)
            try:
                await self._vector_store.delete_by_document(  # type: ignore[call-arg]
                    source_id=event.source_id,
                    external_id=event.external_id,
                    workspace_id=workspace_id,
                )
            except Exception as exc:
                INGESTION_ERRORS_TOTAL.labels(
                    source_type=event.source_type, stage=_STAGE_VECTOR
                ).inc()
                bound.error("stage_delete_vector_error", error=str(exc))
                raise
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


# ---------------------------------------------------------------------------
# Free helpers (small + pure)
# ---------------------------------------------------------------------------


def _tag_entity_workspace(entity: Any, workspace_id: uuid.UUID) -> Any:
    """Stamp ``workspace_id`` onto an extracted entity's ``metadata``.

    The Neo4j adapter resolves the workspace via the first entity's
    ``metadata['workspace_id']`` (see
    ``Neo4jGraphStore._workspace_from_entities``).  The parser's
    ``ExtractedEntity`` does not carry a ``workspace_id`` natively, so
    we set it on every entity defensively.  Mutating the existing
    ``metadata`` dict is safe — extraction is per-document and the
    caller does not retain a reference to the originals after this
    point.
    """
    metadata: dict[str, Any] | None = getattr(entity, "metadata", None)
    if metadata is None:
        # Some duck-typed shapes may not own a metadata attribute; raise
        # so the bug is visible rather than silently dropping ACL.
        raise ValueError("entity_missing_metadata_attribute")
    metadata["workspace_id"] = str(workspace_id)
    return entity


__all__ = ["EntityLinkerProtocol", "IndexWriterProtocol", "IngestionPipeline"]
