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
"""

from __future__ import annotations

import logging
import time
import uuid
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
    HNSW_EF_CONSTRUCT,
    HNSW_M,
    INDEXED_PAYLOAD_FIELDS,
    NAMED_VECTOR_DENSE_PRIMARY,
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
    TIER_WARM,
)
from omniscience_index.stores.qdrant_filters import (
    QdrantFilterBuilder,
    build_end_date_chunks_filter,
    build_retention_eligible_filter,
    build_tombstone_sweep_filter,
    build_warm_archive_filter,
    build_warm_tier_filter,
)

if TYPE_CHECKING:  # Lazy type-only import to avoid a core <-> retrieval cycle.
    from omniscience_retrieval.models import SearchRequest, SearchResult

logger = logging.getLogger(__name__)


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

    @property
    def collection_name(self) -> str:
        """Return the collection name used by this adapter instance."""
        return self._collection_name

    # ------------------------------------------------------------------
    # Collection bootstrap — idempotent
    # ------------------------------------------------------------------

    async def _ensure_collection(self) -> None:
        """Create the collection if missing.  Idempotent."""
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
        """Create payload indexes listed in ADR-0006.  Idempotent."""
        for field_name in INDEXED_PAYLOAD_FIELDS:
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
    ) -> UpsertOutcome:
        """Atomically upsert a document and its chunks into Qdrant.

        Decision table (mirrors pgvector semantics):
        - No existing chunks for (source, external_id)  → insert → action="created"
        - Existing with same content_hash               → no-op   → action="unchanged"
        - Existing with different hash                  → replace → action="updated"

        ``workspace_id`` must be present in every chunk's metadata
        (propagated via ``metadata[workspace_id]``); missing or null
        is rejected at this layer per ADR-0006 §ACL carry-forward.
        """
        workspace_id = _extract_workspace_id(metadata)
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
    ) -> UpsertOutcome:
        """Create or replace the document's chunks as a single logical batch."""
        document_id = existing.document_id if existing else uuid.uuid4()
        doc_version = (existing.doc_version + 1) if existing else 1
        action: Literal["created", "updated"] = "updated" if existing else "created"
        if existing is not None:
            await self._delete_points(list(existing.chunk_point_ids))
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
            )
            for chunk in chunks
        ]
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
    ) -> qm.PointStruct:
        """Translate a ``ChunkPayload`` + doc context into a Qdrant point."""
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
        )
        return qm.PointStruct(
            id=point_id,
            vector={NAMED_VECTOR_DENSE_PRIMARY: embedding},
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
        """Find the logical document identified by (source_id, external_id).

        Reads are scoped to ``workspace_id`` via the filter-builder so
        cross-workspace aliases never leak across the tenant boundary
        (ADR-0006 §ACL carry-forward).
        """
        flt = (
            QdrantFilterBuilder(workspace_id=workspace_id)
            .with_source_ids([source_id])
            .with_external_id(external_id)
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

    async def _delete_points(self, point_ids: list[uuid.UUID]) -> None:
        """Delete points by id.  No filter — caller must have verified scope."""
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
        """Vector-only search, confined to the caller's workspace.

        ADR-0006 §ACL carry-forward: ``workspace_id`` is the
        mandatory must-clause filter; no code path in this method
        can construct a ``Filter`` without it.

        ``as_of`` (issue #134) is the optional ADR-0008 §5 bitemporal
        anchor.  When supplied, the canonical open-closed predicate
        ``valid_from <= as_of AND (valid_to > as_of OR valid_to IS NULL)``
        is layered onto the filter as additional ``must`` clauses; the
        workspace must-clause is never replaced.  When ``None`` (default)
        the filter is byte-identical to the pre-#134 contract — current-
        state reads do not pay the predicate cost.  ``as_of`` precedence
        with ``request.as_of``: the keyword wins so server-side overrides
        stay explicit (mirrors :class:`GraphRAGComposer.search`).
        """
        start = time.monotonic()
        query_vector = await self._embed_query(request.query)
        effective_as_of = as_of if as_of is not None else request.as_of
        flt = self._request_to_filter(
            request=request,
            workspace_id=workspace_id,
            as_of=effective_as_of,
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
        if effective_as_of is not None:
            QDRANT_AS_OF_FILTERED_TOTAL.labels(method="search").inc()
        return _build_search_result(
            scored=hits.points, top_k=request.top_k, duration_ms=duration_ms
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
        """
        builder = QdrantFilterBuilder(workspace_id=workspace_id)
        source_ids = _extract_source_ids(request)
        if source_ids:
            builder = builder.with_source_ids(source_ids)
        if not request.include_tombstoned:
            builder = builder.exclude_tombstoned()
        if as_of is not None:
            builder = builder.with_as_of(as_of)
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
        flt = builder.build()
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
) -> dict[str, Any]:
    """Assemble the Qdrant payload dict for a single chunk point.

    Fields follow ADR-0006 §Schema posture §Required payload fields
    plus doc-level fields needed for document reconstruction
    (external_id, uri, title, content_hash, doc_version).

    Bitemporal fields (ADR-0008 §6, issue #134): every chunk carries
    ``valid_from``, ``valid_to``, ``recorded_at`` so the ``as_of``
    predicate in :class:`QdrantFilterBuilder` is index-backed on every
    read.  Defaults: ``recorded_at = now()``, ``valid_from = recorded_at``,
    ``valid_to = None`` (still-valid sentinel from ADR-0008 §1).  The
    historical-import path with caller-supplied ``valid_*`` is a separate
    sub-issue per #134 non-goals.
    """
    now_iso = datetime.now(UTC).isoformat()
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
        PAYLOAD_VALID_FROM: now_iso,
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


def _build_search_result(
    *, scored: list[qm.ScoredPoint], top_k: int, duration_ms: float
) -> SearchResult:
    """Convert Qdrant scored points to a ``SearchResult`` pydantic model.

    Imported lazily to avoid the core<-retrieval dependency cycle
    (``omniscience_index`` must not statically import from
    ``omniscience_retrieval``).
    """
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
            )
        )
    return SearchResult(
        hits=hits,
        query_stats=QueryStats(
            total_matches_before_filters=len(scored),
            vector_matches=len(scored),
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
    "build_collection_name",
]
