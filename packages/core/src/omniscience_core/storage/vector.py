"""``VectorStore`` protocol: backend-neutral chunk persistence and search.

This module defines the typed contract that the pgvector adapter
(``omniscience_retrieval.adapters.pgvector_vector``) satisfies today
and that the Qdrant adapter (issue #106) will satisfy in Phase 2.

Design rules (ADR-0006 + issue #117)
------------------------------------

1. **Workspace is an invariant, not a parameter.**  ``search`` takes
   ``workspace_id: uuid.UUID`` as a **keyword-only, required**
   parameter.  Adapters MUST confine the search result set to the
   caller's workspace.  See the twin invariant in
   ``omniscience_core.storage.graph.GraphStore``.

2. **Payload shape is a TypedDict.**  ``ChunkPayload`` is the canonical
   on-the-wire shape for a chunk going into the store; adapters
   translate it to their native representation (Chunk ORM row for
   pgvector; Qdrant Point.payload for Qdrant).

3. **No SQLAlchemy dependency here.**  This module imports only stdlib
   and the retrieval response dataclasses — by design, the protocol
   layer is backend-neutral.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
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
    - ``omniscience_retrieval.adapters.pgvector_vector.PgVectorVectorStore``
      (pgvector-backed; today's behaviour).
    - ``omniscience_vector_qdrant.QdrantVectorStore`` (Phase 2, issue
      #106).

    ACL invariant
    -------------
    ``search`` takes ``workspace_id`` as a **keyword-only, required**
    parameter.  Adapters MUST confine result rows to the caller's
    workspace.  The pgvector adapter today does not yet filter search
    by workspace (documented follow-up; see adapter docstring and
    issue tracker) — the protocol is correct so the Qdrant adapter
    can enforce the invariant natively from day one.
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
    ) -> UpsertOutcome:
        """Atomically create or update a document and its chunks.

        Decision table (evaluated inside one transaction):
        - No existing row           → insert + create chunks
        - Existing, same hash       → no-op
        - Existing, different hash  → replace chunks, bump version

        Parameters mirror ``IndexWriter.upsert_document`` for zero-
        behaviour-change compatibility.
        """
        ...

    async def delete_by_document(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
    ) -> bool:
        """Tombstone the document identified by ``(source_id, external_id)``.

        Returns ``True`` when the document existed and was tombstoned;
        ``False`` when no matching active document was found.

        Note: "tombstone" means set ``tombstoned_at`` — hard deletion
        is deferred to ``delete_tombstoned``.  This preserves the
        two-phase deletion semantics the existing pgvector writer
        implements.
        """
        ...

    async def delete_tombstoned(self, older_than: timedelta) -> int:
        """Hard-delete tombstoned documents older than ``older_than``.

        Returns the number of documents removed.  Chunks cascade
        automatically via the FK ``ON DELETE CASCADE`` constraint in
        the pgvector schema.
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
            workspace.  Today's pgvector adapter carries a documented
            gap here (workspace filtering is not yet applied on the
            search path — tracked outside #103); the protocol is
            correct so the Qdrant adapter can enforce the invariant
            natively when it lands (#106).

        Returns
        -------
        ``omniscience_retrieval.models.SearchResult`` — typed as
        ``Any`` on the protocol; narrowed in the adapter.
        """
        ...

    async def count(self, *, source_id: uuid.UUID) -> int:
        """Return the number of active (non-tombstoned) documents for a source.

        Mirrors an admin/observability call-site (freshness worker,
        MCP ``sources`` tool).  Tombstoned rows are excluded.
        """
        ...


__all__ = [
    "ChunkPayload",
    "UpsertOutcome",
    "VectorSearchResult",
    "VectorStore",
]
