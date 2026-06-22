"""``VectorStore`` protocol: backend-neutral chunk persistence and search.

This module defines the typed contract that the Qdrant adapter
(``omniscience_index.stores.qdrant_store``) and PostgresOnlyStore satisfy.

Design rules (ADR-0006 + issue #117)
------------------------------------

1. **Workspace is an invariant, not a parameter.**  ``search`` takes
   ``workspace_id: uuid.UUID`` as a **keyword-only, required**
   parameter.  Adapters MUST confine the search result set to the
   caller's workspace.  See the twin invariant in
   ``omniscience_core.storage.graph.GraphStore``.

2. **Payload shape is a TypedDict.**  ``ChunkPayload`` is the canonical
   on-the-wire shape for a chunk going into the store; adapters
   translate it to their native representation (Qdrant Point.payload
   for the default Qdrant backend).

3. **No SQLAlchemy dependency here.**  This module imports only stdlib
   and the retrieval response dataclasses — by design, the protocol
   layer is backend-neutral.

Stage 4 — enumerate mode (refactor/bitemporal-vector-hybrid)
-------------------------------------------------------------
``enumerate_chunks`` and ``count_enumerate`` bypass HNSW entirely and
use ``scroll`` / ``count(exact=True)`` on payload indexes for 100%
recall on "list all X / count all Y" queries.  Both methods are
workspace-scoped per the ACL invariant (ADR-0006 §ACL).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol, TypedDict, runtime_checkable

# ---------------------------------------------------------------------------
# Payload TypedDict — canonical chunk shape going into the store
# ---------------------------------------------------------------------------


class ChunkPayload(TypedDict, total=False):
    """Typed payload for a single chunk upsert.

    ``total=False`` allows adapters to populate optional fields
    lazily; see ``required`` keys below for the mandatory subset.

    Required keys (validated by adapters at runtime):
        - ord
        - text
        - embedding

    Optional keys:
        - symbol
        - metadata
        - embedding_model
        - embedding_provider
        - parser_version
        - chunker_strategy
    """

    ord: int
    text: str
    embedding: list[float]
    symbol: str | None
    metadata: dict[str, Any]
    embedding_model: str
    embedding_provider: str
    parser_version: str
    chunker_strategy: str


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UpsertOutcome:
    """Outcome of a single ``VectorStore.upsert_chunks`` call.

    Mirrors ``omniscience_index.writer.UpsertResult`` but lives in
    core so adapters need not depend on the index package.
    """

    action: Literal["created", "updated", "unchanged"]
    document_id: uuid.UUID
    chunks_written: int
    doc_version: int


@dataclass
class VectorSearchResult:
    """Backend-neutral container for a search result set.

    Adapters populate ``hits`` with dicts that carry the same shape as
    ``omniscience_retrieval.models.SearchHit``.  The concrete pydantic
    ``SearchHit`` type lives in retrieval to avoid a core<-retrieval
    dependency; adapters that want strict typing should construct
    ``SearchHit`` instances at the retrieval layer.
    """

    hits: list[Any] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorStore(Protocol):
    """Abstract vector-store contract (chunk persistence + search).

    Implementations
    ---------------
    - ``omniscience_index.stores.qdrant_store.QdrantVectorStore`` —
      the sole production backend as of v0.2 (ADR-0006, issue #106).

    ACL invariant
    -------------
    ``search`` takes ``workspace_id`` as a **keyword-only, required**
    parameter.  Adapters MUST confine result rows to the caller's
    workspace.  The Qdrant adapter enforces this natively via a
    payload filter (ADR-0006 §ACL).
    """

    # ------------------------------------------------------------------
    # Write API
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
        """Atomically create or update a document and its chunks.

        Decision table (evaluated inside one transaction):
        - No existing row           → insert + create chunks
        - Existing, same hash       → no-op
        - Existing, different hash  → replace chunks, bump version

        Parameters mirror the legacy pgvector ingestion shape for
        zero-behaviour-change compatibility with the connector layer.
        """
        ...

    async def delete_by_document(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        """Tombstone the document identified by ``(source_id, external_id)``.

        Returns ``True`` when the document existed and was tombstoned;
        ``False`` when no matching active document was found.

        ``workspace_id`` is keyword-only and optional at the protocol
        level for backward-compatibility; the Qdrant adapter requires it
        (raises ``ValueError`` when ``None``) per the ACL invariant
        (ADR-0006 §ACL).

        Note: "tombstone" means set ``tombstoned_at`` — hard deletion
        is deferred to ``delete_tombstoned``.  This preserves the
        two-phase deletion semantics the ingestion layer relies on.
        """
        ...

    async def delete_tombstoned(self, older_than: timedelta) -> int:
        """Hard-delete tombstoned documents older than ``older_than``.

        Returns the number of documents removed.  Chunks cascade
        automatically via the FK ``ON DELETE CASCADE`` constraint in
        the operational-metadata schema.
        """
        ...

    # ------------------------------------------------------------------
    # Read API — search REQUIRES workspace_id (keyword-only)
    # ------------------------------------------------------------------

    async def search(
        self,
        *,
        request: Any,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> Any:
        """Execute a retrieval query, confined to the caller's workspace.

        Parameters
        ----------
        request:
            ``omniscience_retrieval.models.SearchRequest`` instance.
            Typed as ``Any`` on the protocol to avoid a core ->
            retrieval dependency cycle; the adapter signature narrows
            this to ``SearchRequest``.
        workspace_id:
            REQUIRED.  Adapters MUST confine every match to this
            workspace.  The Qdrant adapter enforces the invariant
            natively via a payload filter.
        as_of:
            Optional ADR-0008 §5 bitemporal anchor (issue #134).  When
            supplied, the canonical open-closed predicate
            ``valid_from <= as_of AND (valid_to > as_of OR valid_to IS NULL)``
            narrows the result set to rows valid at ``as_of``.  ``None``
            (default) is the current-state hot path — byte-identical to
            the pre-#134 contract.

        Returns
        -------
        ``omniscience_retrieval.models.SearchResult`` — typed as
        ``Any`` on the protocol; narrowed in the adapter.
        """
        ...

    async def count(
        self,
        *,
        source_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> int:
        """Return the number of active (non-tombstoned) documents for a source.

        Mirrors an admin/observability call-site (freshness worker,
        MCP ``sources`` tool).  Tombstoned rows are excluded.  ``as_of``
        (issue #134) narrows the count to documents valid at the
        supplied bitemporal anchor.
        """
        ...

    # ------------------------------------------------------------------
    # Stats API — issue #111 (workspace-scoped chunk totals)
    # ------------------------------------------------------------------

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
    ) -> int:
        """Return the number of active (non-tombstoned) chunk points.

        Workspace-scoped per the protocol's ACL invariant.  When
        ``source_id`` is supplied the count is further narrowed to that
        source, otherwise the whole workspace is summed.  ``as_of``
        (issue #134) narrows to chunks valid at the supplied timestamp.

        Used by the stats overview endpoint and by the per-source stats
        table.  Tombstoned chunk points are excluded.
        """
        ...

    async def count_chunks_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> dict[str, int]:
        """Return a per-source chunk count map within ``workspace_id``.

        Map keys are the stringified ``source_id`` UUIDs; values are
        non-negative counts of active (non-tombstoned) chunk points.
        Used to assemble the per-source stats table in a single
        backend round-trip rather than N source-id queries.  ``as_of``
        (issue #134) narrows the histogram to chunks valid at the
        supplied bitemporal anchor.
        """
        ...

    # ------------------------------------------------------------------
    # Enumerate API — Stage 4 (refactor/bitemporal-vector-hybrid)
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

        Stage 4 (enumerate mode): bypasses HNSW entirely, uses
        ``scroll`` on payload indexes for 100% recall.  Suitable for
        "list all X / count all Y" questions that must not miss any
        matching document.

        Results are deduplicated by ``document_id`` — the first chunk
        for each logical document is returned with its full payload.

        Workspace scoping is mandatory per ADR-0006 §ACL.

        Parameters
        ----------
        metadata_key:
            One of the indexed enumerate dimensions
            (``service``, ``resource_type``, ``account_id``,
            ``kind``, ``namespace``).
        metadata_value:
            Exact match value for the given dimension.
        source_id:
            Optional source-level narrowing.

        Returns
        -------
        List of payload dicts, one per unique ``document_id``.
        """
        ...

    async def count_enumerate(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> int:
        """Return the exact chunk count matching a payload dimension.

        Stage 4: uses ``count(exact=True)`` for O(index) exact counting.
        Unlike ``count_chunks`` this operates at the chunk level (not the
        document level).  For document-level counts call
        ``enumerate_chunks`` and take ``len(result)``.

        Workspace scoping is mandatory per ADR-0006 §ACL.

        Parameters
        ----------
        metadata_key:
            Enumerate dimension key (``service``, ``kind``, etc.).
        metadata_value:
            Exact match value.
        source_id:
            Optional source-level narrowing.
        """
        ...


__all__ = [
    "ChunkPayload",
    "UpsertOutcome",
    "VectorSearchResult",
    "VectorStore",
]
