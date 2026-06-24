"""Qdrant-backed adapter for the ``VectorStore`` protocol.

Implements issue #106 (Phase 2b of Epic #96) per ADR-0006.  See:

- ``docs/decisions/0006-qdrant-as-vector-store.md`` for the
  authoritative spec (collection layout, payload schema, deployment
  posture, ACL carry-forward).
- ``packages/core/src/omniscience_core/storage/vector.py`` for the
  ``VectorStore`` protocol this class satisfies.
- ``packages/retrieval/src/omniscience_retrieval/adapters/pgvector_vector.py``
  for the parallel pgvector implementation this class must be
  behaviour-compatible with on the contract-test surface.

ACL invariant (non-negotiable, ADR-0006 §ACL carry-forward)
-----------------------------------------------------------

Every read path in this module composes filters through
``QdrantFilterBuilder``, which takes ``workspace_id`` as a required
constructor argument.  Raw ``qdrant_client.models.Filter`` construction
is forbidden outside ``qdrant_filters.py`` and is enforced by a lint
test (see ``tests/test_qdrant_filter_builder_lint.py``).

Stage 1 — bitemporal end-dating on content update
--------------------------------------------------

ADR-0008 §6 / refactor/bitemporal-vector-hybrid:

When a document's ``content_hash`` changes, the old chunk points are
**end-dated** (``valid_to = now()``) instead of hard-deleted.  New
chunk points are inserted with ``valid_from = now()`` and
``valid_to = None``.  The vector store is therefore append-only
versioned, symmetric with the Neo4j graph (which end-dates entities
and edges rather than deleting them).

Consequence for ``as_of`` reads: an ``as_of=T`` search on a document
that was updated after ``T`` will return the OLD chunk version (the
one valid at ``T``), not an empty result.  This closes the cross-store
consistency gap described in ADR-0008 §6.

Current-state reads (``as_of=None``, the hot path) are unaffected in
performance because the filter builder adds ``valid_to IS NULL`` only
when ``with_current_only()`` is called — and the public ``search``
method still uses the pre-Stage-1 filter shape (no ``valid_*``
clauses at all for the default path).

Stage 3 — BM25/RRF sparse + dense hybrid
-----------------------------------------

When ``retrieval_strategy == "hybrid"`` (default) or ``"keyword"``,
the store performs a sparse-vector search alongside (or instead of)
the dense search.  Sparse vectors are built client-side from chunk
text via a simple TF tokenizer (see ``_tokenize_to_sparse``).
``hybrid`` combines dense + sparse results via Reciprocal Rank Fusion
(RRF, ``RRF_K = 60``).  ``keyword`` returns sparse-only results so
callers can request BM25-style exact-name lookup.

Stage 4 — enumerate mode
-------------------------

``enumerate_chunks`` bypasses HNSW entirely and uses
``count(exact=True)`` + ``scroll`` to enumerate all chunks matching
a payload dimension (``metadata.service``, ``metadata.kind``, etc.).
Results are deduplicated by ``document_id``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
import zlib
from collections import Counter as _Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from omniscience_core.storage.vector import (
    ChunkPayload,
    UpsertOutcome,
)
from omniscience_embeddings.base import EmbeddingProvider
from prometheus_client import Counter
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm
from qdrant_client.http.exceptions import UnexpectedResponse

from omniscience_index.stores.qdrant_config import QdrantConfig
from omniscience_index.stores.qdrant_constants import (
    COLLECTION_NAME_PREFIX,
    ENUMERATE_METADATA_PREFIX,
    ENUMERATE_PAYLOAD_INDEXES,
    HNSW_EF_CONSTRUCT,
    HNSW_M,
    INDEXED_PAYLOAD_FIELDS,
    NAMED_VECTOR_DENSE_PRIMARY,
    NAMED_VECTOR_SPARSE_BM25,
    PAYLOAD_CHUNKER_STRATEGY,
    PAYLOAD_CONTENT_HASH,
    PAYLOAD_DOC_VERSION,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EMBEDDING_MODEL,
    PAYLOAD_EMBEDDING_PROVIDER,
    PAYLOAD_EXTERNAL_ID,
    PAYLOAD_INGESTION_RUN_ID,
    PAYLOAD_METADATA,
    PAYLOAD_ORD,
    PAYLOAD_PARSER_VERSION,
    PAYLOAD_RECORDED_AT,
    PAYLOAD_SNAPSHOT_DATE,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_SYMBOL,
    PAYLOAD_TEXT,
    PAYLOAD_TIER,
    PAYLOAD_TITLE,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_URI,
    PAYLOAD_VALID_FROM,
    PAYLOAD_VALID_TO,
    PAYLOAD_WORKSPACE_ID,
    RRF_K,
    SPARSE_CANDIDATE_FACTOR,
    TIER_WARM,
)
from omniscience_index.stores.qdrant_filters import (
    QdrantFilterBuilder,
    build_end_date_chunks_filter,
    build_enumerate_filter,
    build_point_ids_filter,
    build_retention_eligible_filter,
    build_tombstone_sweep_filter,
    build_warm_archive_filter,
    build_warm_tier_filter,
)

if TYPE_CHECKING:  # Lazy type-only import to avoid a core <-> retrieval cycle.
    from omniscience_retrieval.models import SearchRequest, SearchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stopwords for the sparse tokenizer (Stage 3)
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "is",
        "it",
        "to",
        "be",
        "as",
        "at",
        "by",
        "for",
        "on",
        "with",
        "this",
        "that",
        "from",
        "was",
        "are",
        "has",
        "had",
        "not",
        "but",
        "have",
    }
)


# ---------------------------------------------------------------------------
# Telemetry — issue #134 ops visibility
# ---------------------------------------------------------------------------

#: Counter incremented every time a Qdrant read carries the ADR-0008 §5
#: ``as_of`` predicate.  Labelled by ``method`` (one of ``"search"``,
#: ``"count"``, ``"count_chunks"``, ``"count_chunks_by_source"``) so ops
#: can see which read paths exercise the bitemporal filter.  Per #173
#: the ``as_of_kind`` histogram label on
#: ``omniscience_request_duration_seconds`` covers MCP/REST surfaces;
#: this counter is the lower-level adapter signal that the predicate
#: actually reached Qdrant.
QDRANT_AS_OF_FILTERED_TOTAL: Counter = Counter(
    name="omniscience_qdrant_as_of_filtered_total",
    documentation=(
        "Total Qdrant reads that applied the ADR-0008 §5 ``as_of`` "
        "predicate (issue #134). Increments once per read call when "
        "``as_of`` is supplied; current-state reads (``as_of=None``) "
        "are not counted so the metric is a clean signal of historical "
        "read load."
    ),
    labelnames=["method"],
)


# ---------------------------------------------------------------------------
# Embedding provider protocol — minimal shape required by the adapter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DocumentRecord:
    """Reconstructed logical-document view of the Qdrant points.

    Qdrant has no "documents" table — a logical document is the set of
    points that share ``document_id``.  Methods like ``upsert_chunks``
    that need document-level metadata reconstruct it by querying for
    one representative point.
    """

    document_id: uuid.UUID
    content_hash: str
    doc_version: int
    chunk_point_ids: tuple[uuid.UUID, ...]
    tombstoned: bool


# ---------------------------------------------------------------------------
# Collection name builder (deterministic, idempotent)
# ---------------------------------------------------------------------------


def build_collection_name(*, embedding_model: str, dim: int) -> str:
    """Build the collection name for a (model, dim) pair.

    ADR-0006 §Schema posture: one collection per embedding model; the
    collection is uniquely identified by the model id and its
    dimensionality.  The name is deterministic so startup bootstrap is
    idempotent.
    """
    safe_model = embedding_model.replace("/", "__").replace(":", "__")
    return f"{COLLECTION_NAME_PREFIX}__{safe_model}__d{dim}"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class QdrantVectorStore:
    """Qdrant-backed ``VectorStore`` — the Phase-2 adapter (issue #106).

    Satisfies ``omniscience_core.storage.VectorStore`` with native
    payload-aware HNSW, mandatory workspace filtering on every read,
    and idempotent collection bootstrap.

    Concurrency
    -----------
    ``AsyncQdrantClient`` is the asyncio entry point.  It holds an
    internal gRPC channel (or HTTPX client) and is safe to share
    across tasks.  The adapter keeps a single client instance and
    its lifecycle is bound to ``connect`` / ``close``.
    """

    def __init__(
        self,
        *,
        config: QdrantConfig,
        embedding_provider: EmbeddingProvider,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._config = config
        self._embedding_provider = embedding_provider
        self._collection_name = build_collection_name(
            embedding_model=embedding_provider.model_name,
            dim=embedding_provider.dim,
        )
        self._client: AsyncQdrantClient | None = client
        self._bootstrapped: bool = client is not None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the client connection.  Safe to call multiple times."""
        if self._client is None:
            self._client = self._build_client()
        if not self._bootstrapped:
            await self._ensure_collection()
            await self._ensure_payload_indexes()
            self._bootstrapped = True

    async def close(self) -> None:
        """Close the underlying client and release transport resources."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._bootstrapped = False

    def _build_client(self) -> AsyncQdrantClient:
        """Construct the Qdrant client with gRPC-preferred transport.

        ADR-0006 §Transport: gRPC (6334) primary, HTTP (6333) fallback.
        ``qdrant-client`` picks gRPC when ``prefer_grpc=True`` and both
        ports are reachable, and will fall back to HTTP on RPC errors.
        """
        return AsyncQdrantClient(
            host=self._config.host,
            port=self._config.http_port,
            grpc_port=self._config.grpc_port,
            api_key=self._config.api_key,
            https=self._config.https,
            prefer_grpc=self._config.prefer_grpc,
            timeout=int(self._config.timeout_seconds),
        )

    @property
    def _qc(self) -> AsyncQdrantClient:
        """Return the connected client or raise if not connected."""
        if self._client is None:
            raise RuntimeError("QdrantVectorStore: connect() must be awaited first")
        return self._client

    async def get_entity_versions(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, int]:
        """Return {entity_id: version} for all entity chunks in the workspace.

        AP2 — per-entity anti-entropy: entity chunks are stored with
        ``external_id = "entity:<uuid>"`` so we filter by that prefix and
        read the ``doc_version`` payload field (set by ``upsert_chunks``).
        """
        flt = (
            QdrantFilterBuilder(workspace_id=workspace_id)
            .with_chunker_strategy("entity_outbox")
            .build()
        )
        try:
            records = await self._scroll_all(flt=flt, with_payload=True)
            result: dict[uuid.UUID, int] = {}
            for r in records:
                payload = r.payload or {}
                ext_id = payload.get(PAYLOAD_EXTERNAL_ID, "")
                if not str(ext_id).startswith("entity:"):
                    continue
                try:
                    ent_id = uuid.UUID(str(ext_id).removeprefix("entity:"))
                except ValueError:
                    continue
                doc_ver = payload.get(PAYLOAD_DOC_VERSION)
                if doc_ver is not None:
                    result[ent_id] = int(doc_ver)
            return result
        except Exception as e:
            raise RuntimeError(f"Qdrant get_entity_versions failed: {e}") from e

    async def get_entity_content_hashes(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, str]:
        """Return {entity_id: content_hash} for all entity chunks with a hash in the workspace.

        AP2 — per-entity anti-entropy: reads the ``content_hash`` payload field
        from entity-strategy chunks.  Chunks that pre-date AP2 (no content_hash
        in payload) are omitted; the reconcile worker treats them as "not yet
        hashed" and skips hash comparison for those entities.
        """
        flt = (
            QdrantFilterBuilder(workspace_id=workspace_id)
            .with_chunker_strategy("entity_outbox")
            .build()
        )
        try:
            records = await self._scroll_all(flt=flt, with_payload=True)
            result: dict[uuid.UUID, str] = {}
            for r in records:
                payload = r.payload or {}
                ext_id = payload.get(PAYLOAD_EXTERNAL_ID, "")
                if not str(ext_id).startswith("entity:"):
                    continue
                try:
                    ent_id = uuid.UUID(str(ext_id).removeprefix("entity:"))
                except ValueError:
                    continue
                ch = payload.get(PAYLOAD_CONTENT_HASH)
                if ch is not None:
                    result[ent_id] = str(ch)
            return result
        except Exception as e:
            raise RuntimeError(f"Qdrant get_entity_content_hashes failed: {e}") from e

    @property
    def collection_name(self) -> str:
        """Return the collection name used by this adapter instance."""
        return self._collection_name

    # ------------------------------------------------------------------
    # Collection bootstrap — idempotent
    # ------------------------------------------------------------------

    async def _ensure_collection(self) -> None:
        """Create the collection if missing.  Idempotent.

        Stage 3 (refactor/bitemporal-vector-hybrid): the collection is
        created with BOTH a dense named vector (``dense_primary``) AND a
        sparse named vector (``sparse_bm25``) so BM25/RRF hybrid search
        is available without a collection-level migration.

        Existing collections that pre-date Stage 3 lack the sparse
        vector; those are handled by the migration script
        ``scripts/reindex_qdrant_hybrid.py`` which recreates the
        collection with both vectors and re-indexes all points.
        """
        exists = await self._qc.collection_exists(self._collection_name)
        if exists:
            return
        await self._qc.create_collection(
            collection_name=self._collection_name,
            vectors_config={
                NAMED_VECTOR_DENSE_PRIMARY: qm.VectorParams(
                    size=self._embedding_provider.dim,
                    distance=qm.Distance.COSINE,
                )
            },
            sparse_vectors_config={
                NAMED_VECTOR_SPARSE_BM25: qm.SparseVectorParams(
                    index=qm.SparseIndexParams(on_disk=False)
                )
            },
            hnsw_config=qm.HnswConfigDiff(m=HNSW_M, ef_construct=HNSW_EF_CONSTRUCT),
        )
        logger.info(
            "qdrant_collection_created",
            extra={
                "collection": self._collection_name,
                "dim": self._embedding_provider.dim,
                "model": self._embedding_provider.model_name,
            },
        )

    async def _ensure_payload_indexes(self) -> None:
        """Create payload indexes listed in ADR-0006 + enumerate indexes.

        Stage 4: in addition to the core ADR-0006 indexes, each
        ``metadata.<key>`` enumerate dimension gets a KEYWORD payload
        index so payload-filter scans are O(index) not O(collection).
        """
        for field_name in INDEXED_PAYLOAD_FIELDS:
            await self._create_payload_index_if_missing(field_name)
        for field_name in ENUMERATE_PAYLOAD_INDEXES:
            await self._create_payload_index_if_missing(field_name)

    async def _create_payload_index_if_missing(self, field_name: str) -> None:
        """Best-effort payload-index create; already-exists is tolerated."""
        schema = self._payload_schema_for(field_name)
        try:
            await self._qc.create_payload_index(
                collection_name=self._collection_name,
                field_name=field_name,
                field_schema=schema,
            )
        except UnexpectedResponse:
            # Already exists — Qdrant returns 4xx for duplicate index
            # creation; adapter treats as idempotent success.
            logger.debug("payload_index_exists", extra={"field": field_name})

    @staticmethod
    def _payload_schema_for(field_name: str) -> qm.PayloadSchemaType:
        """Map payload field name to its Qdrant index schema type."""
        if field_name in (
            PAYLOAD_RECORDED_AT,
            PAYLOAD_TOMBSTONED_AT,
            PAYLOAD_VALID_FROM,
            PAYLOAD_VALID_TO,
        ):
            return qm.PayloadSchemaType.DATETIME
        # `snapshot_date` is stored as ISO calendar-day string; KEYWORD
        # gives O(1) point-lookup which is the warm-tier read pattern.
        return qm.PayloadSchemaType.KEYWORD

    # ------------------------------------------------------------------
    # Write API — upsert_chunks
    # ------------------------------------------------------------------

    async def upsert_chunks(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[ChunkPayload],
        ingestion_run_id: uuid.UUID | None = None,
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> UpsertOutcome:
        """Atomically upsert a document and its chunks into Qdrant.

        Decision table (mirrors pgvector semantics):
        - No existing chunks for (source, external_id)  → insert → action="created"
        - Existing with same content_hash               → no-op   → action="unchanged"
        - Existing with different hash                  → end-date old → insert new
                                                          → action="updated"

        Stage 1 (bitemporal end-dating): the "different hash" path no longer
        physically deletes old points.  Old points receive ``valid_to = now()``;
        new points are inserted with ``valid_from = now()`` and
        ``valid_to = None``.  The vector store is therefore append-only
        versioned, symmetric with the Neo4j graph (ADR-0008 §6).

        ``workspace_id`` must be present in every chunk's metadata
        (propagated via ``metadata[workspace_id]``); missing or null
        is rejected at this layer per ADR-0006 §ACL carry-forward.
        """
        workspace_id = _extract_workspace_id(metadata)

        checkpoint_id = None
        if version is not None:
            checkpoint_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"checkpoint_{source_id}"))
            points = await self._qc.retrieve(
                collection_name=self._collection_name, ids=[checkpoint_id], with_payload=True
            )
            if points and points[0].payload:
                existing_version = points[0].payload.get("version")
                existing_epoch = points[0].payload.get("epoch")

                should_skip = False
                if existing_version is not None and existing_version >= version:
                    should_skip = True

                if epoch is not None and existing_epoch is not None and epoch > existing_epoch:
                    should_skip = False

                if forced_replay:
                    should_skip = False

                if should_skip:
                    return UpsertOutcome(
                        action="unchanged",
                        document_id=uuid.uuid4(),
                        chunks_written=0,
                        doc_version=0,
                    )

        existing = await self._find_document(
            workspace_id=workspace_id,
            source_id=source_id,
            external_id=external_id,
        )
        if existing is not None and existing.content_hash == content_hash:
            return UpsertOutcome(
                action="unchanged",
                document_id=existing.document_id,
                chunks_written=0,
                doc_version=existing.doc_version,
            )
        return await self._write_document(
            existing=existing,
            workspace_id=workspace_id,
            source_id=source_id,
            external_id=external_id,
            uri=uri,
            title=title,
            content_hash=content_hash,
            metadata=metadata,
            chunks=chunks,
            ingestion_run_id=ingestion_run_id,
            checkpoint_id=checkpoint_id,
            version=version,
            epoch=epoch,
        )

    async def _write_document(
        self,
        *,
        existing: _DocumentRecord | None,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[ChunkPayload],
        ingestion_run_id: uuid.UUID | None,
        checkpoint_id: str | None,
        version: int | None,
        epoch: int | None = None,
    ) -> UpsertOutcome:
        """Create or replace the document's chunks as a single logical batch.

        Stage 1 change: when ``existing`` is not None, the old chunk
        points are end-dated (``valid_to = now()``) rather than deleted.
        New points are then inserted with the fresh content.  The
        ``document_id`` is preserved across versions so downstream
        callers that track document identity by UUID stay stable.
        """
        document_id = existing.document_id if existing else uuid.uuid4()
        doc_version = (existing.doc_version + 1) if existing else 1
        action: Literal["created", "updated"] = "updated" if existing else "created"
        now = datetime.now(UTC)
        if existing is not None:
            # Stage 1 (bitemporal end-dating): end-date old points instead of
            # hard-deleting them so that as_of queries before this update
            # still return the previous chunk version.
            await self._end_date_points(
                point_ids=list(existing.chunk_point_ids),
                workspace_id=workspace_id,
                valid_to=now,
            )
        points = [
            self._chunk_to_point(
                chunk=chunk,
                document_id=document_id,
                workspace_id=workspace_id,
                source_id=source_id,
                external_id=external_id,
                uri=uri,
                title=title,
                content_hash=content_hash,
                doc_version=doc_version,
                metadata=metadata,
                ingestion_run_id=ingestion_run_id,
                valid_from=now,
            )
            for chunk in chunks
        ]
        if checkpoint_id and version is not None:
            points.append(
                qm.PointStruct(
                    id=checkpoint_id,
                    vector={NAMED_VECTOR_DENSE_PRIMARY: [0.0] * self._embedding_provider.dim},
                    payload={
                        "is_checkpoint": True,
                        "source_id": str(source_id),
                        "version": version,
                        "epoch": epoch,
                        "workspace_id": str(workspace_id),
                    },
                )
            )
        if points:
            await self._qc.upsert(collection_name=self._collection_name, points=points, wait=True)
        return UpsertOutcome(
            action=action,
            document_id=document_id,
            chunks_written=len(chunks),
            doc_version=doc_version,
        )

    def _chunk_to_point(
        self,
        *,
        chunk: ChunkPayload,
        document_id: uuid.UUID,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        doc_version: int,
        metadata: dict[str, Any],
        ingestion_run_id: uuid.UUID | None,
        valid_from: datetime | None = None,
    ) -> qm.PointStruct:
        """Translate a ``ChunkPayload`` + doc context into a Qdrant point.

        Stage 3: the point now carries both a dense vector and a sparse
        BM25 vector.  The sparse vector is built client-side from the
        chunk text via ``_tokenize_to_sparse``.
        """
        text, ord_, embedding = _require_chunk_fields(chunk)
        point_id = str(uuid.uuid4())
        payload = _build_chunk_payload(
            text=text,
            ord_=ord_,
            chunk=chunk,
            document_id=document_id,
            workspace_id=workspace_id,
            source_id=source_id,
            external_id=external_id,
            uri=uri,
            title=title,
            content_hash=content_hash,
            doc_version=doc_version,
            metadata=metadata,
            ingestion_run_id=ingestion_run_id,
            valid_from=valid_from,
        )
        sparse = _tokenize_to_sparse(text)
        vectors: dict[str, Any] = {NAMED_VECTOR_DENSE_PRIMARY: embedding}
        if sparse.indices:
            vectors[NAMED_VECTOR_SPARSE_BM25] = sparse
        return qm.PointStruct(
            id=point_id,
            vector=vectors,
            payload=payload,
        )

    # ------------------------------------------------------------------
    # Document lookup (scroll one point to recover doc-level fields)
    # ------------------------------------------------------------------

    async def _find_document(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        external_id: str,
    ) -> _DocumentRecord | None:
        """Find the CURRENT logical document identified by (source_id, external_id).

        Reads are scoped to ``workspace_id`` via the filter-builder so
        cross-workspace aliases never leak across the tenant boundary
        (ADR-0006 §ACL carry-forward).

        Stage 1: adds ``with_current_only()`` so end-dated (historical)
        points from previous content versions are not included in the
        result.  Without this, the writer would see both old end-dated
        points and new current points for the same (source, external_id),
        which would make content-hash comparison unreliable and
        doc_version calculation incorrect.
        """
        flt = (
            QdrantFilterBuilder(workspace_id=workspace_id)
            .with_source_ids([source_id])
            .with_external_id(external_id)
            .with_current_only()
            .build()
        )
        points = await self._scroll_all(flt=flt, with_payload=True)
        if not points:
            return None
        return _record_from_points(points)

    async def _scroll_all(self, *, flt: qm.Filter, with_payload: bool) -> list[qm.Record]:
        """Scroll the whole result set for a filter, paging internally."""
        out: list[qm.Record] = []
        offset: qm.ExtendedPointId | None = None
        page_size = 256
        while True:
            records, next_offset = await self._qc.scroll(
                collection_name=self._collection_name,
                scroll_filter=flt,
                limit=page_size,
                with_payload=with_payload,
                with_vectors=False,
                offset=offset,
            )
            out.extend(records)
            if next_offset is None:
                break
            offset = next_offset
        return out

    async def _end_date_points(
        self,
        *,
        point_ids: list[uuid.UUID],
        workspace_id: uuid.UUID,
        valid_to: datetime,
    ) -> None:
        """Set ``valid_to`` on a specific list of chunk points.

        Stage 1 (bitemporal end-dating): replaces the old
        ``_delete_points`` call in the content-update path.  The
        points remain in the collection but are excluded from current-
        state reads (``with_current_only()``), count, and search
        (which default to the current-state predicate via
        ``as_of=None``).

        The workspace filter is retained even though point IDs are
        globally unique — the lint rule forbids bare ``qm.Filter``
        construction outside ``qdrant_filters.py``.
        """
        if not point_ids:
            return
        valid_to_iso = valid_to.isoformat()
        flt = build_point_ids_filter(
            workspace_id=workspace_id,
            point_ids=[str(p) for p in point_ids],
        )
        await self._qc.set_payload(
            collection_name=self._collection_name,
            payload={PAYLOAD_VALID_TO: valid_to_iso},
            points=qm.FilterSelector(filter=flt),
            wait=True,
        )

    async def _delete_points(self, point_ids: list[uuid.UUID]) -> None:
        """Delete points by id.  No filter — caller must have verified scope.

        NOTE: this method is intentionally kept for the retention worker
        and tombstone-sweep paths.  It is NO LONGER called from the
        content-update write path (Stage 1).  Any new caller should
        prefer ``_end_date_points`` unless retention-grade eviction is
        required.
        """
        if not point_ids:
            return
        await self._qc.delete(
            collection_name=self._collection_name,
            points_selector=qm.PointIdsList(points=[str(p) for p in point_ids]),
            wait=True,
        )

    # ------------------------------------------------------------------
    # Delete API
    # ------------------------------------------------------------------

    async def delete_by_document(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        """Tombstone a document by marking its chunks with ``tombstoned_at``.

        ``workspace_id`` is keyword-only.  It defaults to ``None`` ONLY
        to remain signature-compatible with the ``VectorStore``
        protocol (which predates this call gaining the kwarg); when
        ``None`` this method raises — the caller MUST pass a workspace.
        """
        if workspace_id is None:
            raise ValueError(
                "QdrantVectorStore.delete_by_document requires workspace_id; "
                "cross-workspace tombstoning is forbidden (ADR-0006 §ACL)."
            )
        existing = await self._find_document(
            workspace_id=workspace_id,
            source_id=source_id,
            external_id=external_id,
        )
        if existing is None or existing.tombstoned:
            return False
        await self._qc.set_payload(
            collection_name=self._collection_name,
            payload={PAYLOAD_TOMBSTONED_AT: datetime.now(UTC).isoformat()},
            points=[str(p) for p in existing.chunk_point_ids],
            wait=True,
        )
        return True

    async def end_date_chunks(
        self,
        *,
        source_id: uuid.UUID,
        workspace_id: uuid.UUID,
        snapshot_at: datetime | None = None,
        exclude_external_ids: set[str] | None = None,
    ) -> int:
        """End-date chunks for ``(workspace_id, source_id)`` instead of deleting.

        ADR-0008 §6 + issue #137: under bitemporal-enabled deployment,
        chunks absent from a new snapshot ingestion get their payload
        ``valid_to`` set to ``snapshot_at`` (or ``now()`` when None).
        Hard deletion is gone for the writer path; the retention worker
        (#135 / ADR-0009) is the only remaining DELETE path on Qdrant.

        ``exclude_external_ids`` is the set of chunk ``external_id``s
        present in the new batch — these chunks are NOT end-dated
        because the writer is upserting them with fresh payload.  An
        empty set / ``None`` means "end-date everything still-open in
        ``(workspace, source)``", which is the per-document tombstone
        contract.

        Workspace-scoped via the typed filter-builder
        (:func:`build_end_date_chunks_filter`) per ADR-0006 §ACL — raw
        ``qm.Filter`` construction is forbidden outside
        ``qdrant_filters.py``.

        Returns the count of points whose ``valid_to`` payload was set.
        """
        excl = tuple(sorted(exclude_external_ids)) if exclude_external_ids else ()
        flt = build_end_date_chunks_filter(
            workspace_id=workspace_id,
            source_id=source_id,
            exclude_external_ids=excl,
        )
        # Count first so we have a deterministic return value (the
        # ``set_payload`` op acks rather than returns a count).
        before = int(
            (
                await self._qc.count(
                    collection_name=self._collection_name,
                    count_filter=flt,
                    exact=True,
                )
            ).count
        )
        if before == 0:
            return 0
        valid_to_iso = (snapshot_at or datetime.now(UTC)).isoformat()
        await self._qc.set_payload(
            collection_name=self._collection_name,
            payload={PAYLOAD_VALID_TO: valid_to_iso},
            points=qm.FilterSelector(filter=flt),
            wait=True,
        )
        return before

    async def delete_tombstoned(self, older_than: timedelta) -> int:
        """Hard-delete tombstoned documents older than ``older_than``.

        Because tombstoning is a per-point flag in Qdrant (no
        separate documents table), this counts distinct
        ``document_id`` values among the deleted points.
        """
        cutoff = (datetime.now(UTC) - older_than).isoformat()
        flt = build_tombstone_sweep_filter(cutoff_iso=cutoff)
        records = await self._scroll_all(flt=flt, with_payload=True)
        doc_ids = {r.payload.get(PAYLOAD_DOCUMENT_ID) for r in records if r.payload}
        if records:
            await self._qc.delete(
                collection_name=self._collection_name,
                points_selector=qm.PointIdsList(points=[str(r.id) for r in records]),
                wait=True,
            )
        return len({d for d in doc_ids if d is not None})

    # ------------------------------------------------------------------
    # Read API — search (workspace_id required)
    # ------------------------------------------------------------------

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> SearchResult:
        """Search, dispatched by ``request.retrieval_strategy``.

        Stage 3 (refactor/bitemporal-vector-hybrid):

        - ``"hybrid"`` (default): dense + sparse, merged via RRF.
        - ``"keyword"``: sparse BM25 only — for exact-name lookup.
        - ``"structural"``: graph-anchor scoped dense-only (pure vector,
          same as pre-Stage-3; the graph anchor is resolved by the
          ``GraphRAGComposer`` caller).
        - ``"auto"``: same as ``"hybrid"`` for now; heuristic routing
          is a follow-up issue.

        ADR-0006 §ACL carry-forward: ``workspace_id`` is the
        mandatory must-clause filter; no code path in this method
        can construct a ``Filter`` without it.

        ``as_of`` (issue #134) is the optional ADR-0008 §5 bitemporal
        anchor.  When supplied, the canonical open-closed predicate is
        layered onto all stage filters.
        """
        effective_as_of = as_of if as_of is not None else request.as_of
        strategy = request.retrieval_strategy

        if strategy == "keyword":
            return await self._search_sparse(
                request=request,
                workspace_id=workspace_id,
                as_of=effective_as_of,
            )
        if strategy in ("hybrid", "auto"):
            return await self._search_hybrid_rrf(
                request=request,
                workspace_id=workspace_id,
                as_of=effective_as_of,
            )
        # "structural" and unknown values fall through to dense-only
        # (the caller — GraphRAGComposer — has already applied the
        # graph-anchor source filter to ``request.sources``).
        return await self._search_dense(
            request=request,
            workspace_id=workspace_id,
            as_of=effective_as_of,
        )

    async def _search_dense(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None,
    ) -> SearchResult:
        """Execute a dense (HNSW) vector search."""
        start = time.monotonic()
        query_vector = await self._embed_query(request.query)
        flt = self._request_to_filter(
            request=request,
            workspace_id=workspace_id,
            as_of=as_of,
        )
        hits = await self._qc.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            using=NAMED_VECTOR_DENSE_PRIMARY,
            query_filter=flt,
            limit=request.top_k,
            with_payload=True,
            with_vectors=False,
        )
        duration_ms = (time.monotonic() - start) * 1000.0
        if as_of is not None:
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="search").inc()
        return _build_search_result(
            scored=hits.points, top_k=request.top_k, duration_ms=duration_ms, text_matches=0
        )

    async def _search_sparse(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None,
    ) -> SearchResult:
        """Execute a sparse BM25 search (keyword strategy).

        Uses the ``sparse_bm25`` named vector.  Returns
        ``text_matches`` equal to the number of hits so the caller
        can distinguish keyword results from dense-only results.
        """
        start = time.monotonic()
        sparse_query = _tokenize_to_sparse(request.query)
        if not sparse_query.indices:
            # Empty query after tokenization — return empty result.
            duration_ms = (time.monotonic() - start) * 1000.0
            return _empty_search_result(duration_ms=duration_ms)
        flt = self._request_to_filter(
            request=request,
            workspace_id=workspace_id,
            as_of=as_of,
        )
        hits = await self._qc.query_points(
            collection_name=self._collection_name,
            query=sparse_query,
            using=NAMED_VECTOR_SPARSE_BM25,
            query_filter=flt,
            limit=request.top_k,
            with_payload=True,
            with_vectors=False,
        )
        duration_ms = (time.monotonic() - start) * 1000.0
        if as_of is not None:
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="search").inc()
        n = len(hits.points)
        return _build_search_result(
            scored=hits.points, top_k=request.top_k, duration_ms=duration_ms, text_matches=n
        )

    async def _search_hybrid_rrf(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None,
    ) -> SearchResult:
        """Execute dense + sparse search and merge via Reciprocal Rank Fusion.

        Stage 3 RRF design:
        - Dense candidates: ``top_k * SPARSE_CANDIDATE_FACTOR`` (wider net).
        - Sparse candidates: same.
        - Both searches share the same workspace+as_of filter.
        - RRF formula: score = Σ 1/(k + rank_i) where k = RRF_K = 60.
        - Graph-affinity blend (MERGE_ALPHA) is applied AFTER RRF by the
          ``GraphRAGComposer._run_merge_stage``; this method does not know
          about the anchor subgraph.
        - ``text_matches`` is set to the number of sparse hits so
          ``QueryStats`` surfaces a real count (was always 0 pre-Stage-3).
        """
        start = time.monotonic()
        flt = self._request_to_filter(
            request=request,
            workspace_id=workspace_id,
            as_of=as_of,
        )
        candidate_k = request.top_k * SPARSE_CANDIDATE_FACTOR

        # Dense leg
        query_vector = await self._embed_query(request.query)
        dense_hits = await self._qc.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            using=NAMED_VECTOR_DENSE_PRIMARY,
            query_filter=flt,
            limit=candidate_k,
            with_payload=True,
            with_vectors=False,
        )

        # Sparse leg
        sparse_query = _tokenize_to_sparse(request.query)
        sparse_hits_list: list[qm.ScoredPoint] = []
        if sparse_query.indices:
            sparse_result = await self._qc.query_points(
                collection_name=self._collection_name,
                query=sparse_query,
                using=NAMED_VECTOR_SPARSE_BM25,
                query_filter=flt,
                limit=candidate_k,
                with_payload=True,
                with_vectors=False,
            )
            sparse_hits_list = sparse_result.points

        duration_ms = (time.monotonic() - start) * 1000.0
        if as_of is not None:
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="search").inc()

        merged = _rrf_merge(
            dense=dense_hits.points,
            sparse=sparse_hits_list,
            top_k=request.top_k,
        )
        text_matches = len(sparse_hits_list)
        return _build_search_result(
            scored=merged,
            top_k=request.top_k,
            duration_ms=duration_ms,
            text_matches=text_matches,
        )

    def _request_to_filter(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> qm.Filter:
        """Build the Qdrant filter for a search request.

        ``workspace_id`` is always present.  Optional narrowers
        (sources, tombstone exclusion, ``as_of`` predicate) are layered
        on top.

        Stage 1 (bitemporal end-dating, refactor/bitemporal-vector-hybrid):
        When ``as_of=None`` (current-state read), ``with_current_only()``
        (``valid_to IS NULL``) is added so end-dated historical chunk
        versions do not appear in results.  Before Stage 1 only
        ``exclude_tombstoned`` was applied; this was correct because
        old points were hard-deleted and never survived past their
        replacement.  After Stage 1 they persist with ``valid_to`` set,
        so the current-state filter must explicitly exclude them.

        When ``as_of`` is supplied the open-closed temporal predicate
        ``valid_from <= as_of AND (valid_to > as_of OR valid_to IS NULL)``
        covers the correct generation; ``with_current_only()`` must NOT
        be added because it would exclude end-dated historical versions
        that ARE valid at ``as_of``.
        """
        builder = QdrantFilterBuilder(workspace_id=workspace_id)
        source_ids = _extract_source_ids(request)
        if source_ids:
            builder = builder.with_source_ids(source_ids)
        if not request.include_tombstoned:
            builder = builder.exclude_tombstoned()
        # Stage 1: as_of=None → current-only (valid_to IS NULL);
        # as_of=T → open-closed temporal predicate.
        builder = builder.with_as_of(as_of) if as_of is not None else builder.with_current_only()
        return builder.build()

    async def _embed_query(self, query: str) -> list[float]:
        vectors = await self._embedding_provider.embed([query])
        return vectors[0]

    # ------------------------------------------------------------------
    # Count
    # ------------------------------------------------------------------

    async def count(
        self,
        *,
        source_id: uuid.UUID,
        workspace_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
    ) -> int:
        """Return the number of active (non-tombstoned) documents for a source.

        ``workspace_id`` is keyword-only; see ``delete_by_document`` for
        the rationale on the ``| None`` default and the fail-closed
        check.  ``as_of`` (issue #134) is the optional ADR-0008 §5
        bitemporal anchor — when supplied, only documents valid at
        ``as_of`` are counted.
        """
        if workspace_id is None:
            raise ValueError("QdrantVectorStore.count requires workspace_id (ADR-0006 §ACL).")
        builder = (
            QdrantFilterBuilder(workspace_id=workspace_id)
            .with_source_ids([source_id])
            .exclude_tombstoned()
        )
        if as_of is not None:
            builder = builder.with_as_of(as_of)
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="count").inc()
        else:
            # Current-state: also exclude end-dated historical points
            # so the count reflects only the still-valid generation.
            builder = builder.with_current_only()
        flt = builder.build()
        records = await self._scroll_all(flt=flt, with_payload=True)
        document_ids = {r.payload.get(PAYLOAD_DOCUMENT_ID) for r in records if r.payload}
        return len({d for d in document_ids if d is not None})

    # ------------------------------------------------------------------
    # Stats API (issue #111) — workspace-scoped chunk counts
    # ------------------------------------------------------------------

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
    ) -> int:
        """Count active (non-tombstoned) chunk points within a workspace.

        Uses the native Qdrant ``count`` API, which evaluates the filter
        on the server and returns a single integer — no point payloads
        traverse the wire.  Mandatory ``workspace_id`` is enforced via
        ``QdrantFilterBuilder``.  ``as_of`` (issue #134) is the optional
        ADR-0008 §5 bitemporal anchor — when supplied, only chunks valid
        at ``as_of`` are counted.
        """
        builder = QdrantFilterBuilder(workspace_id=workspace_id).exclude_tombstoned()
        if source_id is not None:
            builder = builder.with_source_ids([source_id])
        if as_of is not None:
            builder = builder.with_as_of(as_of)
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="count_chunks").inc()
        else:
            builder = builder.with_current_only()
        flt = builder.build()
        result = await self._qc.count(
            collection_name=self._collection_name,
            count_filter=flt,
            exact=True,
        )
        return int(result.count)

    # ------------------------------------------------------------------
    # Enumerate API — Stage 4
    # ------------------------------------------------------------------

    async def enumerate_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        """Return all current chunks matching a payload dimension.

        Stage 4 (enumerate mode, refactor/bitemporal-vector-hybrid):

        Bypasses HNSW entirely.  Uses ``scroll`` on payload indexes for
        O(index) retrieval — suited for "list all X / count all Y"
        questions that require 100% recall (HNSW has non-trivial miss
        probability).

        Results are deduplicated by ``document_id`` (NOT by URI) to
        avoid presenting the same logical document twice when it has
        multiple chunks.  The first chunk for each ``document_id`` is
        returned with its full payload.

        Workspace scoping is mandatory per ADR-0006 §ACL.

        Parameters
        ----------
        metadata_key:
            One of the indexed enumerate dimensions
            (``service``, ``resource_type``, ``account_id``,
            ``kind``, ``namespace``).  Must match a key in
            ``ENUMERATE_METADATA_INDEX_KEYS``.
        metadata_value:
            Exact match value for the given dimension.
        source_id:
            Optional source-level narrowing.

        Returns
        -------
        List of payload dicts, one per unique ``document_id``.
        """
        payload_field = f"{ENUMERATE_METADATA_PREFIX}.{metadata_key}"
        flt = build_enumerate_filter(
            workspace_id=workspace_id,
            metadata_key=payload_field,
            metadata_value=metadata_value,
            source_id=source_id,
        )
        records = await self._scroll_all(flt=flt, with_payload=True)
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for rec in records:
            payload = rec.payload or {}
            doc_id = payload.get(PAYLOAD_DOCUMENT_ID)
            if doc_id is None or doc_id in seen:
                continue
            seen.add(doc_id)
            out.append(dict(payload))
        return out

    async def count_enumerate(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> int:
        """Return the exact count of documents matching a payload dimension.

        Stage 4: uses ``count(exact=True)`` for O(index) exact counting.
        Unlike ``count_chunks`` this counts at the chunk level (not the
        document level); for document-level counts call ``enumerate_chunks``
        and take ``len(result)``.
        """
        payload_field = f"{ENUMERATE_METADATA_PREFIX}.{metadata_key}"
        flt = build_enumerate_filter(
            workspace_id=workspace_id,
            metadata_key=payload_field,
            metadata_value=metadata_value,
            source_id=source_id,
        )
        result = await self._qc.count(
            collection_name=self._collection_name,
            count_filter=flt,
            exact=True,
        )
        return int(result.count)

    # ------------------------------------------------------------------
    # Retention worker support (ADR-0009 §5, issue #135)
    # ------------------------------------------------------------------
    #
    # Every method below is workspace-scoped via the typed filter-builder
    # helpers from ``qdrant_filters.py``.  Raw ``qm.Filter`` construction
    # is forbidden here per the lint rule (ADR-0006 §ACL carry-forward).

    async def count_retention_eligible(
        self,
        *,
        workspace_id: uuid.UUID,
        cutoff: datetime,
    ) -> int:
        """Count chunks with ``recorded_at < cutoff`` in this workspace.

        Used by the dry-run reporter and the live worker to size the
        Qdrant-side eviction batch per ADR-0009 §5.
        """
        flt = build_retention_eligible_filter(
            workspace_id=workspace_id,
            cutoff_iso=cutoff.isoformat(),
        )
        result = await self._qc.count(
            collection_name=self._collection_name,
            count_filter=flt,
            exact=True,
        )
        return int(result.count)

    async def mark_retention_warm(
        self,
        *,
        workspace_id: uuid.UUID,
        cutoff: datetime,
    ) -> int:
        """Tag eligible chunks with ``tier="warm"`` + ``snapshot_date``.

        ADR-0009 §5: warm chunks carry a ``tier="warm"`` payload field
        and a ``snapshot_date`` mirrored from the chunk's ``recorded_at``
        UTC calendar day.  This is the Qdrant analogue of the Neo4j
        :EntitySnapshot:Daily projection.

        The set-payload call is workspace-scoped via the filter-builder.
        Returns the count of points whose payload was updated; this is
        an approximation (Qdrant returns an operation acknowledgement
        rather than an exact mutation count) and we re-count via the
        retention-eligible filter on the same scope, less the still-hot
        cohort.
        """
        flt = build_retention_eligible_filter(
            workspace_id=workspace_id,
            cutoff_iso=cutoff.isoformat(),
        )
        # Stamp a per-point snapshot_date keyed off recorded_at.  Qdrant
        # does not support computed payloads on set_payload, so we read
        # the eligible records once, group by UTC day, and write each
        # day's payload separately.
        records = await self._scroll_payload_only(
            flt=flt,
            include_fields=[PAYLOAD_RECORDED_AT, PAYLOAD_TIER],
        )
        marked = 0
        # Group eligible point ids by snapshot day.
        by_day: dict[str, list[str]] = {}
        for rec in records:
            payload = rec.payload or {}
            # Skip already-marked warm points (idempotent re-run).
            if payload.get(PAYLOAD_TIER) == TIER_WARM:
                continue
            recorded_at = payload.get(PAYLOAD_RECORDED_AT)
            if not isinstance(recorded_at, str):
                continue
            try:
                day = datetime.fromisoformat(recorded_at).date().isoformat()
            except ValueError:
                continue
            by_day.setdefault(day, []).append(str(rec.id))
        for day, point_ids in by_day.items():
            if not point_ids:
                continue
            await self._qc.set_payload(
                collection_name=self._collection_name,
                payload={
                    PAYLOAD_TIER: TIER_WARM,
                    PAYLOAD_SNAPSHOT_DATE: day,
                },
                points=qm.PointIdsList(points=cast("list[int | str | uuid.UUID]", point_ids)),
                wait=True,
            )
            marked += len(point_ids)
        return marked

    async def delete_retention_archive(
        self,
        *,
        workspace_id: uuid.UUID,
        snapshot_date: date,
    ) -> int:
        """Hard-delete warm chunks for one (workspace_id, snapshot_date).

        ADR-0009 §5: re-embedding from archive is not supported in v1,
        so chunks past the warm-to-archive boundary are evicted from
        Qdrant entirely.  Caller is the retention worker, AFTER the
        Neo4j archive parquet write and warm row delete have succeeded.
        """
        flt = build_warm_archive_filter(
            workspace_id=workspace_id,
            snapshot_date_iso=snapshot_date.isoformat(),
        )
        before = int(
            (
                await self._qc.count(
                    collection_name=self._collection_name,
                    count_filter=flt,
                    exact=True,
                )
            ).count
        )
        if before == 0:
            return 0
        await self._qc.delete(
            collection_name=self._collection_name,
            points_selector=qm.FilterSelector(filter=flt),
            wait=True,
        )
        return before

    async def count_chunks_by_tier(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Return {'hot': int, 'warm': int} chunk counts for the workspace.

        Used to populate ``omniscience_graph_records_total`` per
        ADR-0009 §8.  Hot is approximated as "tier field is absent" —
        legacy chunks pre-#135 have no tier field and are hot by
        construction; #131-era chunks may stamp ``tier=hot`` explicitly.
        """
        all_flt = QdrantFilterBuilder(workspace_id=workspace_id).exclude_tombstoned().build()
        # Workspace-scoped warm-tier filter; the helper lives in the
        # sanctioned filter-builder module so the lint guard
        # (test_qdrant_filter_builder_lint.py) stays green.
        tier_only_warm = build_warm_tier_filter(workspace_id=workspace_id)
        total = await self._qc.count(
            collection_name=self._collection_name, count_filter=all_flt, exact=True
        )
        warm = await self._qc.count(
            collection_name=self._collection_name, count_filter=tier_only_warm, exact=True
        )
        warm_count = int(warm.count)
        total_count = int(total.count)
        return {"hot": max(total_count - warm_count, 0), "warm": warm_count}

    async def _scroll_payload_only(
        self,
        *,
        flt: qm.Filter,
        include_fields: list[str],
    ) -> list[qm.Record]:
        """Page through ``flt``, returning only ``include_fields`` payload."""
        out: list[qm.Record] = []
        offset: qm.ExtendedPointId | None = None
        page_size = 256
        while True:
            records, next_offset = await self._qc.scroll(
                collection_name=self._collection_name,
                scroll_filter=flt,
                limit=page_size,
                with_payload=qm.PayloadSelectorInclude(include=include_fields),
                with_vectors=False,
                offset=offset,
            )
            out.extend(records)
            if next_offset is None:
                break
            offset = next_offset
        return out

    async def count_chunks_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> dict[str, int]:
        """Per-source histogram of active chunk points within a workspace.

        Streams payloads (only the ``source_id`` field) and aggregates
        client-side: Qdrant has no group-by primitive in the Python
        client.  The pull is bounded by the workspace and excludes
        tombstoned points so the working-set size is the live chunk
        count, not the historical total.  ``as_of`` (issue #134) narrows
        the histogram to chunks valid at the supplied timestamp.
        """
        builder = QdrantFilterBuilder(workspace_id=workspace_id).exclude_tombstoned()
        if as_of is not None:
            builder = builder.with_as_of(as_of)
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="count_chunks_by_source").inc()
        else:
            builder = builder.with_current_only()
        flt = builder.build()
        offset: qm.ExtendedPointId | None = None
        page_size = 1000
        counts: dict[str, int] = {}
        while True:
            records, next_offset = await self._qc.scroll(
                collection_name=self._collection_name,
                scroll_filter=flt,
                limit=page_size,
                with_payload=qm.PayloadSelectorInclude(include=[PAYLOAD_SOURCE_ID]),
                with_vectors=False,
                offset=offset,
            )
            for r in records:
                payload = r.payload or {}
                src = payload.get(PAYLOAD_SOURCE_ID)
                if src is None:
                    continue
                key = str(src)
                counts[key] = counts.get(key, 0) + 1
            if next_offset is None:
                break
            offset = next_offset
        return counts


# ---------------------------------------------------------------------------
# Free functions (small, pure, easy to unit-test)
# ---------------------------------------------------------------------------


def _extract_workspace_id(metadata: dict[str, Any]) -> uuid.UUID:
    """Read and validate ``workspace_id`` from document metadata.

    Rejects missing / ``None`` / non-UUID values.  This is the
    enforcement point for ADR-0006 §ACL "Require ``workspace_id`` as
    a non-null payload field on every point".
    """
    raw = metadata.get("workspace_id")
    if raw is None:
        raise ValueError(
            "QdrantVectorStore upsert rejected: metadata['workspace_id'] "
            "is required and must be a non-null UUID (ADR-0006 §ACL)."
        )
    if isinstance(raw, uuid.UUID):
        return raw
    if isinstance(raw, str):
        return uuid.UUID(raw)
    raise TypeError(f"workspace_id must be UUID or str, got {type(raw).__name__}")


def _extract_source_ids(request: SearchRequest) -> list[uuid.UUID]:
    """Resolve ``request.sources`` (names) to source-id UUIDs when possible."""
    sources = request.sources or []
    out: list[uuid.UUID] = []
    for s in sources:
        try:
            out.append(uuid.UUID(s))
        except (ValueError, TypeError):
            # Source names (non-UUID) are not supported at the Qdrant
            # layer — retrieval orchestration must resolve names to
            # IDs before calling search.  Silently skipping a bad
            # token would be an ACL-adjacent smell; raise instead.
            raise ValueError(
                "QdrantVectorStore.search: request.sources must contain "
                "source-id UUIDs; name resolution is a retrieval-layer "
                "concern."
            ) from None
    return out


def _require_chunk_fields(chunk: ChunkPayload) -> tuple[str, int, list[float]]:
    """Enforce the (ord, text, embedding) required keys on a chunk payload."""
    try:
        ord_ = chunk["ord"]
        text = chunk["text"]
        embedding = chunk["embedding"]
    except KeyError as exc:
        raise ValueError(f"chunk payload missing required key: {exc}") from exc
    return text, ord_, embedding


def _build_chunk_payload(
    *,
    text: str,
    ord_: int,
    chunk: ChunkPayload,
    document_id: uuid.UUID,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    external_id: str,
    uri: str,
    title: str | None,
    content_hash: str,
    doc_version: int,
    metadata: dict[str, Any],
    ingestion_run_id: uuid.UUID | None,
    valid_from: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the Qdrant payload dict for a single chunk point.

    Fields follow ADR-0006 §Schema posture §Required payload fields
    plus doc-level fields needed for document reconstruction
    (external_id, uri, title, content_hash, doc_version).

    Bitemporal fields (ADR-0008 §6, issue #134): every chunk carries
    ``valid_from``, ``valid_to``, ``recorded_at`` so the ``as_of``
    predicate in :class:`QdrantFilterBuilder` is index-backed on every
    read.  Defaults: ``recorded_at = now()``, ``valid_from = recorded_at``
    (or caller-supplied), ``valid_to = None`` (still-valid sentinel from
    ADR-0008 §1).
    """
    # When ``valid_from`` is supplied by the caller (the write path always
    # passes ``now`` from ``_write_document``), reuse it as ``recorded_at``
    # so the two timestamps are byte-identical.  This satisfies the
    # contract assertion ``valid_from == recorded_at`` on fresh upserts
    # (ADR-0008 §6, issue #134, ``test_upsert_populates_valid_from_and_null_valid_to``).
    # Two separate ``datetime.now(UTC)`` calls produce microsecond skew.
    _now = valid_from if valid_from is not None else datetime.now(UTC)
    now_iso = _now.isoformat()
    valid_from_iso = now_iso
    return {
        PAYLOAD_WORKSPACE_ID: str(workspace_id),
        PAYLOAD_SOURCE_ID: str(source_id),
        PAYLOAD_DOCUMENT_ID: str(document_id),
        PAYLOAD_EXTERNAL_ID: external_id,
        PAYLOAD_URI: uri,
        PAYLOAD_TITLE: title,
        PAYLOAD_CONTENT_HASH: content_hash,
        PAYLOAD_DOC_VERSION: doc_version,
        PAYLOAD_ORD: ord_,
        PAYLOAD_TEXT: text,
        PAYLOAD_SYMBOL: chunk.get("symbol"),
        PAYLOAD_METADATA: dict(chunk.get("metadata", {})),
        PAYLOAD_EMBEDDING_MODEL: chunk.get("embedding_model", ""),
        PAYLOAD_EMBEDDING_PROVIDER: chunk.get("embedding_provider", ""),
        PAYLOAD_PARSER_VERSION: chunk.get("parser_version", ""),
        PAYLOAD_CHUNKER_STRATEGY: chunk.get("chunker_strategy", ""),
        PAYLOAD_INGESTION_RUN_ID: (
            str(ingestion_run_id) if ingestion_run_id is not None else None
        ),
        PAYLOAD_TOMBSTONED_AT: None,
        PAYLOAD_RECORDED_AT: now_iso,
        PAYLOAD_VALID_FROM: valid_from_iso,
        PAYLOAD_VALID_TO: None,
        "doc_metadata": dict(metadata),
    }


def _record_from_points(points: list[qm.Record]) -> _DocumentRecord:
    """Reduce a set of chunk points to a logical ``_DocumentRecord``."""
    first = points[0]
    payload = first.payload or {}
    doc_id_raw = payload.get(PAYLOAD_DOCUMENT_ID)
    if not isinstance(doc_id_raw, str):
        raise ValueError("qdrant point missing document_id in payload")
    return _DocumentRecord(
        document_id=uuid.UUID(doc_id_raw),
        content_hash=str(payload.get(PAYLOAD_CONTENT_HASH, "")),
        doc_version=int(payload.get(PAYLOAD_DOC_VERSION, 0)),
        chunk_point_ids=tuple(_coerce_point_id(p.id) for p in points),
        tombstoned=payload.get(PAYLOAD_TOMBSTONED_AT) is not None,
    )


def _coerce_point_id(pid: qm.ExtendedPointId) -> uuid.UUID:
    """Coerce a Qdrant point id to ``uuid.UUID``; reject non-UUID ids."""
    if isinstance(pid, uuid.UUID):
        return pid
    if isinstance(pid, str):
        return uuid.UUID(pid)
    raise TypeError(f"unexpected point id type: {type(pid).__name__}")


# ---------------------------------------------------------------------------
# Sparse vector tokenizer (Stage 3)
# ---------------------------------------------------------------------------


def _tokenize_to_sparse(text: str) -> qm.SparseVector:
    """Convert text to a sparse TF vector for BM25-style search.

    Algorithm:
    1. Lowercase + split on non-alphanumeric characters.
    2. Remove stopwords and single-character tokens.
    3. Count term frequencies.
    4. Map tokens to integer indices via a process-stable hash
       (crc32(token) mod SPARSE_VOCAB_SIZE).  Collisions are rare
       at 2^20 slots and acceptable for retrieval purposes.
       NOTE: the builtin ``hash()`` MUST NOT be used here — string
       hashing is salted per-process (PYTHONHASHSEED), so an index-time
       token and a query-time token would map to different slots in
       different processes and never match. crc32 is deterministic.
    5. Normalise values to [0, 1] (max-TF normalisation).

    This approximates BM25 without a corpus-level IDF pass.  For the
    intended queries (exact service names, error strings, resource IDs)
    term frequency alone is sufficient because rare tokens are naturally
    discriminative.

    Returns a ``qm.SparseVector`` with ``indices`` and ``values``.
    Empty text returns an empty sparse vector.
    """
    tokens = re.split(r"[^a-z0-9]+", text.lower())
    counts: _Counter[int] = _Counter()
    for tok in tokens:
        if len(tok) <= 1 or tok in _STOPWORDS:
            continue
        idx = zlib.crc32(tok.encode("utf-8")) % _SPARSE_VOCAB_SIZE
        counts[idx] += 1
    if not counts:
        return qm.SparseVector(indices=[], values=[])
    max_tf = max(counts.values())
    indices = sorted(counts)
    values = [counts[i] / max_tf for i in indices]
    return qm.SparseVector(indices=indices, values=values)


#: Vocabulary size for the sparse hash-based tokenizer.
#: 2^20 = 1_048_576 slots.  Collision probability for two random tokens
#: is ~1e-6; for a typical 200-word chunk the expected collision count
#: is < 0.02.  Qdrant stores only non-zero entries, so large vocab size
#: does not waste space.
_SPARSE_VOCAB_SIZE: int = 1 << 20


# ---------------------------------------------------------------------------
# RRF merge (Stage 3)
# ---------------------------------------------------------------------------


def _rrf_merge(
    *,
    dense: list[qm.ScoredPoint],
    sparse: list[qm.ScoredPoint],
    top_k: int,
) -> list[qm.ScoredPoint]:
    """Merge dense and sparse hit lists via Reciprocal Rank Fusion.

    Formula: score(d) = Σ_i 1 / (RRF_K + rank_i(d))
    where rank is 1-based and only lists in which ``d`` appears
    contribute to the sum.

    Points are identified by their string point ID.  The payload from
    the higher-scoring list is carried through; for points appearing in
    both lists, the dense payload wins (richer metadata).

    The returned list is sorted by descending RRF score and sliced to
    ``top_k``.
    """
    scores: dict[str, float] = {}
    payloads: dict[str, dict[str, Any]] = {}

    def _accumulate(hits: list[qm.ScoredPoint]) -> None:
        for rank, sp in enumerate(hits, start=1):
            pid = str(sp.id)
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
            if pid not in payloads and sp.payload:
                payloads[pid] = sp.payload

    _accumulate(dense)
    _accumulate(sparse)

    ranked = sorted(scores, key=lambda pid: scores[pid], reverse=True)[:top_k]
    # Re-assemble ScoredPoint objects with RRF scores.
    out: list[qm.ScoredPoint] = []
    for pid in ranked:
        # Find the original ScoredPoint to use as a template (prefer dense).
        sp_template = next(
            (sp for sp in dense if str(sp.id) == pid),
            next((sp for sp in sparse if str(sp.id) == pid), None),
        )
        if sp_template is None:
            continue
        out.append(
            qm.ScoredPoint(
                id=sp_template.id,
                version=sp_template.version,
                score=scores[pid],
                payload=payloads.get(pid, sp_template.payload),
                vector=None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Search result builders
# ---------------------------------------------------------------------------


def _build_search_result(
    *,
    scored: list[qm.ScoredPoint],
    top_k: int,
    duration_ms: float,
    text_matches: int = 0,
) -> SearchResult:
    """Convert Qdrant scored points to a ``SearchResult`` pydantic model.

    Imported lazily to avoid the core<-retrieval dependency cycle
    (``omniscience_index`` must not statically import from
    ``omniscience_retrieval``).

    Stage 3: ``text_matches`` is now populated with the real sparse-hit
    count so ``QueryStats`` carries a meaningful value.
    """
    from datetime import UTC, datetime

    from omniscience_retrieval.models import (
        ChunkLineage,
        Citation,
        QueryStats,
        SearchHit,
        SearchResult,
        SourceInfo,
    )

    hits: list[SearchHit] = []
    for sp in scored[:top_k]:
        payload = sp.payload or {}
        hits.append(
            SearchHit(
                chunk_id=_coerce_point_id(sp.id),
                document_id=uuid.UUID(cast("str", payload.get(PAYLOAD_DOCUMENT_ID))),
                score=float(sp.score),
                text=str(payload.get(PAYLOAD_TEXT, "")),
                source=SourceInfo(
                    id=uuid.UUID(cast("str", payload.get(PAYLOAD_SOURCE_ID))),
                    name=str(payload.get(PAYLOAD_SOURCE_ID)),
                    type="qdrant",
                ),
                citation=Citation(
                    uri=str(payload.get(PAYLOAD_URI, "")),
                    title=payload.get(PAYLOAD_TITLE),
                    indexed_at=_parse_datetime(payload.get(PAYLOAD_RECORDED_AT)),
                    doc_version=int(payload.get(PAYLOAD_DOC_VERSION, 0)),
                ),
                lineage=ChunkLineage(
                    ingestion_run_id=_parse_opt_uuid(payload.get(PAYLOAD_INGESTION_RUN_ID)),
                    embedding_model=str(payload.get(PAYLOAD_EMBEDDING_MODEL, "")),
                    embedding_provider=str(payload.get(PAYLOAD_EMBEDDING_PROVIDER, "")),
                    parser_version=str(payload.get(PAYLOAD_PARSER_VERSION, "")),
                    chunker_strategy=str(payload.get(PAYLOAD_CHUNKER_STRATEGY, "")),
                ),
                metadata=_safe_dict(payload.get(PAYLOAD_METADATA)),
                applied_version=int(payload.get(PAYLOAD_DOC_VERSION, 0)),
                staleness=max(
                    0.0,
                    (
                        datetime.now(UTC) - _parse_datetime(payload.get(PAYLOAD_RECORDED_AT))
                    ).total_seconds(),
                )
                if payload.get(PAYLOAD_RECORDED_AT)
                else None,
            )
        )

    versions = [h.applied_version for h in hits if h.applied_version is not None]
    min_applied_version = min(versions) if versions else None

    return SearchResult(
        hits=hits,
        query_stats=QueryStats(
            total_matches_before_filters=len(scored),
            vector_matches=len(scored) - text_matches,
            text_matches=text_matches,
            duration_ms=duration_ms,
        ),
        min_applied_version=min_applied_version,
    )


def _empty_search_result(*, duration_ms: float) -> SearchResult:
    """Return an empty ``SearchResult`` (sparse tokenization yielded nothing)."""
    from omniscience_retrieval.models import QueryStats, SearchResult

    return SearchResult(
        hits=[],
        query_stats=QueryStats(
            total_matches_before_filters=0,
            vector_matches=0,
            text_matches=0,
            duration_ms=duration_ms,
        ),
    )


def _parse_datetime(raw: Any) -> datetime:
    """Parse an ISO-8601 string from payload.  Falls back to ``now`` on None."""
    if isinstance(raw, str):
        return datetime.fromisoformat(raw)
    if isinstance(raw, datetime):
        return raw
    return datetime.now(UTC)


def _parse_opt_uuid(raw: Any) -> uuid.UUID | None:
    """Parse an optional UUID field from a payload value."""
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    if isinstance(raw, str):
        return uuid.UUID(raw)
    return None


def _safe_dict(raw: Any) -> dict[str, Any]:
    """Return ``raw`` as a dict or an empty dict when it is not mappable."""
    if isinstance(raw, dict):
        return cast("dict[str, Any]", raw)
    return {}


__all__ = [
    "QdrantConfig",
    "QdrantVectorStore",
    "_rrf_merge",
    "_tokenize_to_sparse",
    "build_collection_name",
]
